import datetime
import logging.config
import os
import time
from collections import deque
import shutil
from threading import Thread, Event
from libs.SysUtil import SysUtil
import csv, json
import traceback

try:
    logging.config.fileConfig("logging.ini")
    logging.getLogger("paramiko").setLevel(logging.WARNING)
except:
    pass

try:
    from sense_hat import SenseHat
except Exception as e:
    logging.warning("Couldnt import sensehat: {}".format(str(e)))

try:
    import Adafruit_DHT
except Exception as e:
    logging.warning("Couldnt import Adafruit_DHT: {}".format(str(e)))

try:
    import telegraf
except Exception as e:
    logging.error("Couldnt import Telegraf, not sending metrics: {}".format(str(e)))


def round_to_1dp(n):
    return round(n, 1)


class Sensor(Thread):
    """
    Sensor base.
    To use this class you need to override 'get_measurement()' so that it returns a tuple of the measurements that match
    the headers defined in the data_headers classvar.
    by default it will write 5 files, rolling 24 hour files (csv, tsv & json) and all time files that are appended to
    (csv & tsv only)
    """
    accuracy = 1
    data_headers = tuple()
    timestamp_format = "%Y-%m-%dT%H:%M:%S"

    def __init__(self, identifier: str,
                 config: dict = None,
                 queue: deque = None,
                 write_out: bool = True,
                 interval: int = 60,
                 **kwargs):
        # identifier is NOT OPTIONAL!
        super().__init__(name=identifier)
        print("Thread started {}: {}".format(self.__class__, identifier))
        # data headers need to be set
        if queue is None:
            queue = deque(tuple(), 256)
        self.communication_queue = queue
        self.logger = logging.getLogger(identifier)
        self.stopper = Event()
        self.identifier = identifier
        if config:
            interval = config.get("interval", interval)
        # interval in seconds
        self.interval = interval
        # chunking interval in number of datapoints
        dlen = int(86400 / interval)

        # setup a deque of measurements
        self.measurements = deque(maxlen=dlen)
        self.write_out = write_out

        out_dir = os.path.join(os.getcwd(), "sensors", self.identifier)
        self.output_dir = config.get("output_dir", out_dir)
        if write_out:
            if not os.path.exists(self.output_dir):
                os.makedirs(self.output_dir)
        self.current_capture_time = datetime.datetime.now()
        self.failed = list()

    @staticmethod
    def timestamp(tn: datetime.datetime) -> str:
        """
        creates a properly formatted timestamp.
        :param tn: datetime to format to timestream timestamp string
        :return:
        """
        return tn.strftime('%Y_%m_%d_%H_%M_%S')

    @staticmethod
    def time2seconds(t: datetime) -> int:
        """
        converts a datetime to an integer of seconds since epoch
        """
        try:
            return int(t.timestamp())
        except:
            # only implemented in python3.3
            # this is an old compatibility thing
            return t.hour * 60 * 60 + t.minute * 60 + t.second

    @property
    def timestamped_filename(self) -> str:
        """
        builds a timestamped image basename without extension from a datetime.
        :param time_now:
        :return: string image basename
        """
        return '{sensor_name}_{timestamp}'.format(sensor_name=self.identifier,
                                                  timestamp=Sensor.timestamp(self.current_capture_time))

    @property
    def time_to_measure(self) -> bool:
        """
        filters out times for mesauring, returns True by default
        returns False if the conditions where the sensor should NOT capture are met.
        :return:
        """
        # data capture interval
        if not (self.time2seconds(self.current_capture_time) % self.interval < Sensor.accuracy):
            return False
        return True

    def stop(self):
        """
        stops the thread.
        :return:
        """
        self.stopper.set()

    def communicate_with_updater(self):
        """
        communication member. This is meant to send some metadata to the updater thread.
        :return:
        """
        try:
            data = dict(
                name=self.identifier,
                last_measure=self.current_capture_time.isoformat(),
                identifier=self.identifier,
                failed=self.failed
            )
            self.communication_queue.append(data)
            self.failed = list()
        except Exception as e:
            self.logger.error("thread communication error: {}".format(str(e)))

    def write_daily_rolling(self):
        """
        writes full rolling daily daita files.
        :param rows:
        :return:
        """
        try:
            fn = os.path.join(self.output_dir, "{}-daily".format(self.identifier))
            csvf, tsvf, jsonf = fn + ".csv", fn + ".tsv", fn + ".json"

            with open(csvf, 'w', newline='') as csvfile, open(tsvf, 'w', newline='') as tsvfile, open(jsonf, 'w',
                                                                                                      newline='') as jsonfile:
                writer = csv.writer(csvfile, dialect=csv.excel)
                writer.writerow(("datetime", *self.data_headers))
                writer.writerows(self.measurements)
                writer = csv.writer(tsvfile, dialect=csv.excel_tab)
                writer.writerow(("datetime", *self.data_headers))
                writer.writerows(self.measurements)
                d = dict()
                for k in self.data_headers:
                    d[k] = list()
                d['datetime'] = []
                for measurement in self.measurements:
                    for idx, m in enumerate(measurement[:len(d.keys())]):
                        header = "datetime"
                        if idx != 0:
                            header = self.data_headers[idx - 1]
                        d[header].append(m)
                jsonfile.write(json.dumps(d))
        except Exception as e:
            self.logger.error("Error writing daily rolling data {}".format(str(e)))

    def append_to_alltime(self, measurement: tuple):
        """
        appends the measurement to the csv and tsv files.

        :param measurement:
        :return:
        """
        try:
            fn = os.path.join(self.output_dir, "{}-lastday".format(self.identifier))
            fn2 = os.path.join(self.output_dir, "{}-alltime".format(self.identifier))
            csvf, tsvf = fn + ".csv", fn + ".tsv"
            csvf2, tsvf2 = fn2 + ".csv", fn2 + ".tsv"
            self.rotate(csvf, tsvf)

            def create_with_headers(path, delimiter=","):
                # write the headers if the files are new.
                if not os.path.exists(path):
                    with open(path, 'w') as f:
                        f.write(delimiter.join(("datetime", *self.data_headers)) + "\n")

            create_with_headers(csvf)
            create_with_headers(csvf2)
            create_with_headers(tsvf, delimiter='\t')
            create_with_headers(tsvf2, delimiter='\t')

            def append_measurement(fn, delimiter=","):
                with open(fn, 'a') as f:
                    f.write(delimiter.join(str(x) for x in measurement) + "\n")

            append_measurement(csvf)
            append_measurement(csvf2)
            append_measurement(tsvf, delimiter='\t')
            append_measurement(tsvf2, delimiter='\t')

        except Exception as e:
            self.logger.error("Error appending measurement to the all time data: {}".format(str(e)))

    def rotate(self, csvf, tsvf):
        def last_line(f):
            f.seek(-1024, 2)
            return f.readlines()[-1].decode()

        rotatecsv, rotatetsv = False, False
        try:
            with open(csvf, 'rb') as f:
                lastd = datetime.datetime.strptime(last_line(f).split(",")[0])
                if lastd.day != datetime.date.today().day:
                    rotatecsv = True

            if rotatecsv:
                shutil.move(csvf, csvf.replace("lastday", lastd.strftime(self.timestamp_format)))
        except:
            # cannot parse datetime because the last line is the header or file doesnt exist
            pass

        try:
            with open(tsvf, 'rb') as f:
                lastd = datetime.datetime.strptime(last_line(f).split("\t")[0])
                if lastd.day != datetime.date.today().day:
                    rotatetsv = True
            if rotatetsv:
                shutil.move(tsvf, tsvf.replace("lastday", lastd.strftime(self.timestamp_format)))
        except:
            # cannot parse datetime because the last line is the header or file doesnt exist
            pass

    def run(self):
        """
        run method.
        used for threaded sensors
        :return:
        """
        while True and not self.stopper.is_set():
            self.current_capture_time = datetime.datetime.now()
            # checking if enabled and other stuff
            if self.time_to_measure:
                try:
                    measurement = self.get_measurement()
                    try:
                        telegraf_client = telegraf.TelegrafClient(host="localhost", port=8092)
                        telegraf_client.metric("env_sensors", measurement)
                        self.logger.info("Sensors: {}".format(str(measurement)))
                    except Exception as exc:
                        self.logger.error("Couldnt communicate with telegraf client. {}".format(str(exc)))
                    # make ordered list of the data for writing. to disk.
                    m = [measurement[k] for k in self.data_headers]
                    self.measurements.append([self.current_capture_time.strftime(self.timestamp_format), *measurement])
                    self.append_to_alltime(self.measurements[-1])
                    self.write_daily_rolling()
                except Exception as e:
                    self.logger.critical("Sensor data error - {}".format(str(e)))
                # make sure we cannot record twice.
                time.sleep(Sensor.accuracy * 2)

            time.sleep(0.1)

    def get_measurement(self) -> dict:
        """
        override this method with the method of collecting measurements from the sensor
        should return a dict

        :return: dict of measurements and their names
        :rtype: dict
        """
        return dict()


"""
TODO: make conviron "sensor" to do the monitoring in a more regular .
"""


class DHTMonitor(Sensor):
    """
    Data logger class for DHT11, DHT22 & AM2302 GPIO temperature & humidity sensors from Adafruit.

    supply the identifier and the gpio pi that the sensor is connected to, along with the type of sensor.
    defaults to pin 14, DHT22
    """

    data_headers = ('humidity', "temperature")

    def __init__(self, identifier, pin: int = 14, sensor_type="AM2302", **kwargs):
        self.pin = pin
        sensor_args = {
            11: Adafruit_DHT.DHT11,
            22: Adafruit_DHT.DHT22,
            2302: Adafruit_DHT.AM2302,
            "11": Adafruit_DHT.DHT11,
            "22": Adafruit_DHT.DHT22,
            "2302": Adafruit_DHT.AM2302,
            "DHT11": Adafruit_DHT.DHT11,
            "DHT22": Adafruit_DHT.DHT22,
            "AM2302": Adafruit_DHT.AM2302,
        }
        self.sensor_type = sensor_args.get(sensor_type, Adafruit_DHT.AM2302)
        super().__init__(identifier, **kwargs)

    def get_measurement(self) -> dict:
        """
        gets data from the DHT22
        """
        try:

            measurement = Adafruit_DHT.read_retry(self.sensor_type, self.pin)
            return {key: round_to_1dp(value) for key, value in zip(self.data_headers, measurement)}
        except Exception as e:
            self.logger.error("Couldnt get data, {}".format(str(e)))

            return {_: None for _ in self.data_headers}

from .Chamber import ConvironTelNetController
class ConvironChamberSensor(Sensor):
    data_headers = ("temp_set", "humidity_set", "temp_recorded", "humidity_recorded", "par")

    def __init__(self, identifier, config, *args, **kwargs):
        self.controller = ConvironTelNetController(config['telnet'])
        self.temperature_multiplier = config.get("temperature_multiplier", 10.0)
        super().__init__(identifier, **kwargs)

    def get_measurement(self):
        measurement = dict()
        for _ in range(10):
            try:
                measurement.update(self.controller.get_values())
                # collect chamber sensor metrics
                if type(measurement.get("temp_recorded")) is float:
                    measurement['temp_recorded'] /= self.temperature_multiplier
                if type(measurement.get("temp_set")) is float:
                    measurement['temp_set'] /= self.temperature_multiplier
                return measurement
            except Exception as e:
                traceback.print_exc()
                self.logger.warning("Couldnt collect chamber sensor metric retrying: {}".format(str(e)))
                print("Failed, retrying ({}/10)".format(_))
        else:
            print("Totally failed getting chamber metrics")
            self.logger.error("Totally failed getting chamber metrics")
        return measurement


class SenseHatMonitor(Sensor):
    """
    Data logger class for Astro Pi Sensehat
    No need to supply anything except the identifier as the SenseHad uses some kind of black sorcery to work it out.
    """

    data_headers = ("temperature", "humidity", "pressure")

    def __init__(self, identifier: str = None, *args, **kwargs):
        self.sensehat = SenseHat()
        self.display_str = "Init Sensors..."
        self.sensehat.show_message(self.display_str)
        super().__init__(identifier, **kwargs)

    def show_data(self, measurement):
        """
        displays the data on the osd.
        
        :param measurement: meausrement to display
        :return:
        """
        try:
            message_str = "T:{temperature:.2f} H:{humidity:.2f} P:{pressure:.2f}"
            self.sensehat.show_message(message_str.format(**measurement))
        except Exception as e:
            self.logger.error(str(e))

    def get_measurement(self) -> dict:
        """
        get measurements for sensehat
        :return:
        """
        try:
            measurement = [self.sensehat.temperature, self.sensehat.humidity, self.sensehat.pressure]
            return {key: round_to_1dp(value) for key, value in zip(self.data_headers, measurement)}
        except Exception as e:
            self.logger.error("Couldnt get data, {}".format(str(e)))
            return {_: None for _ in self.data_headers}

#
# class ThreadedSensor(Sensor, Thread):
#     """
#     threaded implementation of the sensor cclass.
#     """
#
#     def __init__(self, identifier, *args, **kwargs):
#         Thread.__init__(self, name=identifier)
#         self.daemon = True
#         print("Threaded started {}: {}".format(self.__class__, identifier))
#
#
# class ThreadedSenseHat(SenseHatMonitor, ThreadedSensor):
#     """
#     threaded implementation for the AstroPI SenseHat
#     """
#
#     def __init__(self, identifier, *args, **kwargs):
#         super().__init__(identifier,  *args, **kwargs)
#         super(ThreadedSensor, self).__init__(identifier, *args, **kwargs)
#
#
# class ThreadedDHT(DHTMonitor, ThreadedSensor):
#     """
#     threaded implementation for the Adafruit DHT/AM GPIO sensor module
#     """
#     def __init__(self, identifier,  *args, **kwargs):
#         super().__init__(identifier, *args, **kwargs)
#         super(ThreadedSensor, self).__init__(identifier, *args, **kwargs)