#!/usr/bin/env python3

from adafruit_servokit import ServoKit
import argparse
from configparser import ConfigParser
import json
import logging
import paho.mqtt.client as mqtt
import re
import time


class Location(object):
    """Object representing a location on the clockface, and which event patterns should match it"""
    def __init__(self, name, pattern, angle):
        self.log = logging.getLogger(self.__class__.__name__)
        self.name = name
        self.pattern = pattern
        self.angle = angle

    def matches(self, region_name):
        self.log.debug('Seeing if pattern {0} matches region {1}'.format(repr(self.pattern), repr(region_name)))
        if self.pattern is not None:
            return re.search(self.pattern, region_name)
        return False


class Locations(object):
    """Container for all the locations"""
    def __init__(self, config):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self.traveling = Location('traveling', None, self.getConfigInt('travelingangle'))
        self.unknown = Location('unknown', None, self.getConfigInt('unknownangle'))
        self.lost = Location('lost', None, self.getConfigInt('lostangle'))
        self.mortal_peril = Location('mortal peril', None, self.getConfigInt('mortalperilangle'))
        locations = list(filter(lambda x: re.match('location\d+$', x), self.config['locations'].keys()))
        locations.sort()
        self.locations = []
        for loc in locations:
            self.locations.append(Location(self.getConfig(loc), self.getConfig(loc+"pattern"), self.getConfigInt(loc+"angle")))

    def getConfigInt(self, key):
        return int(self.getConfig(key))

    def getConfig(self, key):
        return self.config['locations'][key]

    def getLoc(self, event):
        if event['_type'] != 'transition':
            return None

        if event['event'] == 'leave':
            return self.traveling

        for loc in self.locations:
            if loc.matches(event['desc']):
                return loc
        return self.unknown

    def __iter__(self):
        locs = [self.traveling, self.unknown, self.lost, self.mortal_peril]
        locs.extend(self.locations)
        for x in locs:
            yield x


class Person(object):
    """Representation of a person to be tracked"""
    def __init__(self, name, user, device, servo):
        self.name = name
        self.user = user
        self.device = device,
        self.servo = servo


class Clock(object):
    def __init__(self, configFilePath, servoTest=False):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config_file_path = configFilePath
        self.servo_test = servoTest
        self.initialize()
        self.running = True

    def initialize(self):
        self.readConfig()
        self.setupPeople()
        self.setupServos()
        if self.servo_test:
            self.startupTest()
        self.setupMQTT()

    def readConfig(self):
        self.config = ConfigParser()
        self.config.read(self.config_file_path)
        self.locations = Locations(self.config)

    def setupPeople(self):
        people = list(filter(lambda x: re.match('person\d+$', x), self.config.sections()))
        people.sort()
        self.people = {}
        for person in people:
            section = self.config[person]
            self.people[section['username']+'/'+section['deviceid']] = Person(section['name'], section['username'], section['deviceid'], int(section['servo']))

    def setupServos(self):
        self.servos = ServoKit(channels=int(self.config['servos']['channels']))
        for person in self.people.values():
            num = person.servo
            min_pulse_width = int(self.config['servos']['servo{0}minpw'.format(num)])
            max_pulse_width = int(self.config['servos']['servo{0}maxpw'.format(num)])
            self.servos.servo[num].set_pulse_width_range(min_pulse_width, max_pulse_width)

    def startupTest(self):
        servo_numbers = [person.servo for person in self.people.values()]

        self.log.info('Sweep tests for all servos')
        for angle in [0, 90, 180]:
            for num in servo_numbers:
                    self.log.debug('Angle {0} for servo {1}'.format(angle, num))
                    self.servos.servo[num].angle = angle
                    time.sleep(0.5)

        for location in self.locations:
            for num in servo_numbers:
                self.log.info('Testing pointing servo {0} to {1} at angle {2}'.format(num, location.name, location.angle))
                self.servos.servo[num].angle = location.angle
            time.sleep(1)

        for num in servo_numbers:
            self.log.debug('Resetting servo {0} to 90'.format(num))
            self.servos.servo[num].angle = 90

    def setupMQTT(self):
        self.log.info('Initializing MQTT broker connection')
        self.broker_connected = False
        self.broker = mqtt.Client()
        self.broker.on_connect = self.onBrokerConnect
        self.broker.on_message = self.onBrokerMessage

        if 'tls' in self.config['mqtt'] and self.config['mqtt']['tls'] == 'true':
            self.log.debug('Setting TLS encryption on for MQTT')
            self.broker.tls_set()

        if 'user' in self.config['mqtt'] and 'password' in self.config['mqtt']:
            self.log.debug('Connecting as user {0}'.format(self.config['mqtt']['user']))
            self.broker.username_pw_set(self.config['mqtt']['user'], self.config['mqtt']['password'])

        self.broker.connect(self.config['mqtt']['hostname'], int(self.config['mqtt']['port']))

    def onBrokerConnect(self, client, userdata, flags, rc):
        self.log.info('MQTT broker connected with code {0}'.format(str(rc)))

        # will resubscribe on reconnect
        client.subscribe('owntracks/+/+/event')

    def onBrokerMessage(self, client, userdata, msg):
        self.log.debug('Got message topic {0} with payload {1}'.format(msg.topic, msg.payload))
        event = json.loads(msg.payload)
        if event['_type'] != 'transition':
            return

        owntracks, user, device, msgtype = msg.topic.split('/')
        ident = '{0}/{1}'.format(user, device)
        person = None
        for pattern in self.people.keys():
            self.log.debug('Checking to see if pattern "{0}" matches identifier "{1}"'.format(pattern, ident))
            if re.search(pattern, ident):
                person = self.people[pattern]
                self.log.info('Message looks like it is from {0}'.format(person.name))
                break
        if person is None:
            self.log.warning('Got MQTT msg for unknown user/device "{0}"'.format(ident))
            return
        loc = self.locations.getLoc(event)
        self.setLoc(person, loc)

    def setLoc(self, person, loc):
        if loc.name == 'traveling':
            self.log.info('{0} is traveling'.format(person.name))
        else:
            self.log.info('{0} is in {1}'.format(person.name, loc.name))

        self.servos.servo[person.servo].angle = loc.angle

    def loop(self):
        # self.log.debug('Starting loop')
        self.broker.loop()

def main():
    parser = argparse.ArgumentParser('clock.py')
    parser.add_argument('--debug', action='store_true', help='Debugging output')
    parser.add_argument('--config', default='config.ini', help='Config file')
    parser.add_argument('--servo-test', action='store_true', help='Run servo test sequence')

    args = parser.parse_args()

    level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=level)

    clock = Clock(args.config, args.servo_test)

    while clock.running:
        clock.loop()

if __name__ == '__main__':
    main()
