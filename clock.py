#!/usr/bin/env python3

from adafruit_servokit import ServoKit
import argparse
from configparser import ConfigParser
import json
import logging
import paho.mqtt.client as mqtt
import re
import signal
import sqlite3
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
        self.unknown = Location('in an unknown location', None, self.getConfigInt('unknownangle'))
        self.lost = Location('lost', None, self.getConfigInt('lostangle'))
        self.mortal_peril = Location('in mortal peril', None, self.getConfigInt('mortalperilangle'))
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
    """Main clock object.  Defines the behavior of the clock as a whole."""
    def __init__(self, configFilePath, servoTest=False, min_reconnect_interval=1, max_reconnect_interval=120):
        self.log = logging.getLogger(self.__class__.__name__)
        self.min_reconnect_interval = min_reconnect_interval
        self.max_reconnect_interval = max_reconnect_interval
        self.reconnect_interval = self.min_reconnect_interval
        self.config_file_path = configFilePath
        self.servo_test = servoTest
        self.initialize()
        self.running = True

    def initialize(self):
        self.readConfig()
        self.setupLocations()
        self.setupPeople()
        self.setupServos()
        if self.servo_test:
            self.startupTest()
        self.setupDatabase()
        self.setupMQTT()
        signal.signal(signal.SIGHUP, self.hupHandler)

    def hupHandler(self, signum, frame):
        self.log.info('Got HUP, reinitializing')
        if self.broker_connected:
            self.log.debug('Disconnecting from broker')
            self.broker.disconnect()
        self.readConfig()
        self.setupLocations()
        self.setupPeople()
        self.setupServos()
        if self.servo_test:
            self.startupTest()
        self.setupDatabase()
        # We DON'T want to rerun the MQTT setup.. it will reconnect automatically

    def readConfig(self):
        self.log.debug('reading config file from {0}'.format(self.config_file_path))
        self.config = ConfigParser()
        self.config.read(self.config_file_path)

    def setupLocations(self):
        self.log.debug('Setting up locations')
        self.locations = Locations(self.config)

    def setupPeople(self):
        self.log.debug('Setting up people')
        people = list(filter(lambda x: re.match('person\d+$', x), self.config.sections()))
        people.sort()
        self.log.debug('Found {0} distinct people defined in config'.format(len(people)))
        self.people = {}
        for person in people:
            section = self.config[person]
            self.people[section['username']+'/'+section['deviceid']] = Person(section['name'],
                                                                              section['username'],
                                                                              section['deviceid'],
                                                                              int(section['servo']))

    def setupServos(self):
        self.log.debug('Setting up servos')
        section = self.config['servos']
        channels=int(section['channels'])
        self.servos = ServoKit(channels=channels)
        for num in range(channels):
            min_pulse_width_name = 'servo{0}minpw'.format(num)
            max_pulse_width_name = 'servo{0}maxpw'.format(num)
            if min_pulse_width_name in section and max_pulse_width_name in section:
                min_pulse_width = int(section[min_pulse_width_name])
                max_pulse_width = int(section[max_pulse_width_name])
                self.log.debug('Setting custom pulse width range to ({0}, {1}) on servo {2}'.format(min_pulse_width, max_pulse_width, num))
                self.servos.servo[num].set_pulse_width_range(min_pulse_width, max_pulse_width)

    def setupDatabase(self):
        dbfile = 'state.sqlite'
        if 'database' in self.config and 'statefile' in self.config['database']:
            dbfile = self.config['database']['statefile']
        self.state_db = sqlite3.connect(dbfile)
        self.setStateFromDB()

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

    def setupMQTT(self):
        self.log.info('Initializing MQTT broker connection')
        self.broker_connected = False
        self.broker_log = logging.getLogger('MQTT')
        self.broker = mqtt.Client()
        self.broker.on_connect = self.onBrokerConnect
        self.broker.on_disconnect = self.onBrokerDisconnect
        self.broker.on_log = self.onBrokerLog
        self.broker.on_message = self.onBrokerMessage

        if 'tls' in self.config['mqtt'] and self.config['mqtt']['tls'] == 'true':
            self.log.debug('Setting TLS encryption on for MQTT')
            self.broker.tls_set()

        if 'user' in self.config['mqtt'] and 'password' in self.config['mqtt']:
            self.log.debug('Connecting as user {0}'.format(self.config['mqtt']['user']))
            self.broker.username_pw_set(self.config['mqtt']['user'], self.config['mqtt']['password'])

        self.broker.connect(self.config['mqtt']['hostname'], int(self.config['mqtt']['port']))

    def onBrokerConnect(self, client, userdata, flags, rc):
        self.broker_connected = True
        self.log.info('MQTT broker connected with code {0}'.format(str(rc)))

        # will resubscribe on reconnect
        client.subscribe('owntracks/+/+/event')

    def onBrokerDisconnect(self, client, userdata, rc):
        self.broker_connected = False
        if rc != 0:
            self.log.warning('MQTT broker disconnected with status {0}'.format(rc))
            try:
                self.broker.reconnect()
            except Exception as e:
                self.log.error('While trying to reconnect, got exception {0}'.format(repr(e)))
        else:
            self.log.info('MQTT broker disconnected as expected')

    def onBrokerLog(self, client, userdata, level, buf):
        if level == mqtt.MQTT_LOG_DEBUG:
            self.broker_log.debug(buf)
        elif level in [mqtt.MQTT_LOG_INFO, mqtt.MQTT_LOG_NOTICE]:
            self.broker_log.info(buf)
        elif level == mqtt.MQTT_LOG_WARNING:
            self.broker_log.warning(buf)
        elif level == mqtt.MQTT_LOG_ERR:
            self.broker_log.error(buf)
        else:
            self.log.warning('Unknown MQTT log level {0}'.format(level))
            self.broker_log.warning(buf)

    def onBrokerMessage(self, client, userdata, msg):
        self.log.debug('Got message topic {0} with payload {1}'.format(msg.topic, msg.payload))
        event = json.loads(msg.payload)
        if event['_type'] != 'transition':
            return

        owntracks, user, device, msgtype = msg.topic.split('/')
        ident = '{0}/{1}'.format(user, device)
        person = self.findPerson(ident)
        if person is None:
            self.log.warning('Got MQTT msg for unknown user/device "{0}"'.format(ident))
            return
        loc = self.locations.getLoc(event)
        self.setLoc(person, loc)
        self.saveState(ident, loc.name, loc.angle)

    def findPerson(self, ident):
        person = None
        for pattern in self.people.keys():
            self.log.debug('Checking to see if pattern "{0}" matches identifier "{1}"'.format(pattern, ident))
            if re.search(pattern, ident):
                person = self.people[pattern]
                self.log.debug('Message looks like it is from {0}'.format(person.name))
                break
        return person

    def setLoc(self, person, loc):
        self.log.info('{0} is {1}'.format(person.name, loc.name))

        self.servos.servo[person.servo].angle = loc.angle

    def setStateFromDB(self):
        self.log.debug('Restoring location info from state db')
        try:
            results = self.state_db.execute('SELECT ident, location_name, location_angle, timestamp FROM locations')
            for row in results:
                ident, loc_name, loc_angle, timestamp = row
                person = self.findPerson(ident)
                if person is not None:
                    self.log.info('{0} was last seen {1} (angle {2}) at {3}'.format(person.name, loc_name, loc_angle, 
                                                                                time.asctime(time.localtime(timestamp))))
                    self.servos.servo[person.servo].angle = loc_angle
                else:
                    self.log.error('Unable to find person with ident {0}'.format(ident))
        except sqlite3.OperationalError as e:
            if 'no such table' in str(e):
                self.createLocationsTable()
            else:
                self.log.error('Got unexpected error from database: {0}'.format(repr(e)))

    def saveState(self, ident, name, angle, timestamp=None):
        if timestamp is None:
            timestamp = time.time()

        try_again = True
        attempt_counter = 0
        max_attempts = 3

        while try_again and attempt_counter < max_attempts:
            attempt_counter += 1
            try:
                self.state_db.execute('INSERT INTO locations(ident, location_name, location_angle, timestamp) VALUES (?, ?, ?, ?);',
                                        [ident, name, angle, timestamp])
                self.state_db.commit();
                try_again = False
            except Exception as e:
                if 'no such table' in str(e):
                    self.createLocationsTable()
                else:
                    self.log.error('Got {0} while trying to save state'.format(repr(e)))
                    try_again = False

        if attempt_counter >= max_attempts:
            self.log.error('Unable to save state after {0} attempts'.format(attempt_counter))
        else:
            self.log.debug('Saved state for {0} on attempt {1}'.format(ident, attempt_counter))

    def createLocationsTable(self):
        self.log.info('Attempting to create locations table in state database');
        try:
            sql_statements = [
                """
                CREATE TABLE IF NOT EXISTS location_history (
                    ident TEXT,
                    location_name TEXT,
                    location_angle INT,
                    timestamp INT
                );""",
                """
                CREATE INDEX IF NOT EXISTS location_history_ident_ts 
                    ON location_history(ident, timestamp);
                """,
                """
                CREATE VIEW IF NOT EXISTS latest_ident_update 
                    AS SELECT ident, max(timestamp) AS timestamp
                        FROM location_history GROUP BY ident;
                """,
                """
                CREATE VIEW IF NOT EXISTS locations
                    AS SELECT lh.*
                        FROM location_history lh
                        JOIN latest_ident_update liu
                          ON (lh.ident = liu.ident and lh.timestamp = liu.timestamp);
                """,
                """
                CREATE TRIGGER IF NOT EXISTS save_location_history
                    INSTEAD OF INSERT ON locations
                    FOR EACH ROW
                    BEGIN
                        INSERT INTO location_history(ident, location_name, location_angle, timestamp)
                            VALUES (NEW.ident, NEW.location_name, NEW.location_angle, NEW.timestamp);
                    END;
                """,
            ]
            for stmt in sql_statements:
                self.state_db.execute(stmt);
            return True
        except Exception as e:
            self.log.error('Unable to create database: {0}'.format(repr(e)))
            return False

    def loop(self):
        # self.log.debug('Starting loop')
        rc = self.broker.loop()
        if rc == mqtt.MQTT_ERR_CONN_LOST:
            self.log.error('Connection lost, trying to reconnect')
            try:
                self.broker.reconnect()
            except Exception as e:
                self.log.error('Caught exception {0}'.format(repr(e)))
                time.sleep(self.reconnect_interval)
                self.reconnect_interval = min(self.max_reconnect_interval, 2*self.reconnect_interval)
        elif rc != 0:
            self.log.warning('Loop returned error code {0}'.format(rc))
        else:
            self.reconnect_interval = self.min_reconnect_interval

def main():
    parser = argparse.ArgumentParser('clock.py')
    parser.add_argument('--debug', action='store_true', help='Debugging output')
    parser.add_argument('--config', default='config.ini', help='Config file')
    parser.add_argument('--servo-test', action='store_true', help='Run servo test sequence')

    args = parser.parse_args()

    level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s %(message)s', level=level)

    clock = Clock(args.config, args.servo_test)

    while clock.running:
        clock.loop()

if __name__ == '__main__':
    main()
