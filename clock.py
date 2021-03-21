#!/usr/bin/env python3

from adafruit_servokit import ServoKit
import argparse
from configparser import ConfigParser
import logging
import re
import time

class Location(object):
    def __init__(self, name, pattern, angle):
        self.name = name
        self.pattern = pattern
        self.angle = angle

    def matches(self, region_name):
        if self.pattern is not None:
            return re.search(self.pattern, region_name)
        return False


class Locations(object):
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
    def __init__(self, name, user, device, servo):
        self.name = name
        self.user = user
        self.device = device,
        self.servo = servo

class Clock(object):
    def __init__(self, configFilePath):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config_file_path = configFilePath
        self.initialize()
        self.loop()

    def initialize(self):
        self.readConfig()
        self.setupPeople()
        self.setupServos()
        self.startupTest()

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
            self.people[section['username']] = Person(section['name'], section['username'], section['deviceid'], int(section['servo']))

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
                time.sleep(1.5)

       for location in self.locations:
           for num in servo_numbers:
               self.log.info('Testing pointing servo {0} to {1} at angle {2}'.format(num, location.name, location.angle))
               self.servos.servo[num].angle = location.angle
               time.sleep(1)

       for num in servo_numbers:
           self.log.debug('Resetting servo {0} to 90'.format(num))
           self.servos.servo[num].angle = 90

    def loop(self):
        pass

def main():
    parser = argparse.ArgumentParser('clock.py')
    parser.add_argument('--debug', action='store_true', help='Debugging output')
    parser.add_argument('--config', default='config.ini', help='Config file')

    args = parser.parse_args()

    level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(level=level)

    clock = Clock(args.config)

if __name__ == '__main__':
    main()
