import json
from multiprocessing import Process
import os
import sys
import datetime
import traceback
import uuid
import logging
from logging.handlers import RotatingFileHandler

import argparse
import boto
from boto.sqs.message import RawMessage
import smart_open


BATCH_SIZE = 10
MAX_SIZE = 262144
SKIP_SQS = False

logger = logging.getLogger('EntityPublisher')


def init_logging(stdout_enabled=True):
    root_logger = logging.getLogger()
    log_file_name = './publisher.log'
    log_formatter = logging.Formatter(fmt='%(asctime)s | %(processName)s | %(name)s | %(levelname)s | %(message)s',
                                      datefmt='%m/%d/%Y %I:%M:%S %p')

    rotating_file = logging.handlers.RotatingFileHandler(filename=log_file_name,
                                                         mode='a',
                                                         maxBytes=104857600,
                                                         backupCount=10)
    rotating_file.setFormatter(log_formatter)
    rotating_file.setLevel(logging.INFO)

    root_logger.addHandler(rotating_file)
    root_logger.setLevel(logging.INFO)

    if stdout_enabled:
        stdout_logger = logging.StreamHandler(sys.stdout)
        stdout_logger.setFormatter(log_formatter)
        stdout_logger.setLevel(logging.INFO)
        root_logger.addHandler(stdout_logger)


def attach_queue(sqs_conn, queue_name):
    try:
        queue = sqs_conn.get_queue(queue_name)

        if not queue:
            queue = sqs_conn.create_queue(queue_name)

        return queue

    except Exception, e:
        logger.info(traceback.format_exc())
        raise e


def time_now():
    return unix_time(datetime.datetime.utcnow())


def unix_time(dt):
    epoch = datetime.utcfromtimestamp(0)
    td = dt - epoch

    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10 ** 6) / 10 ** 6


def parse_args():
    parser = argparse.ArgumentParser(description='Apigee BaaS Loader - Publisher')

    parser.add_argument('-t', '--threads',
                        help='The number of threads to run over the file',
                        type=int,
                        default=4)

    parser.add_argument('-q', '--queue_name',
                        help='The queue name to send messages to.  If not specified the filename is used',
                        default='entities',
                        type=str)

    parser.add_argument('--config',
                        help='The number of threads to run over the file',
                        type=str,
                        default='%s/frankenloader.json' % os.getenv("HOME"))

    parser.add_argument('-f', '--filename',
                        help='The filename to load',
                        required=True,
                        type=str)

    parser.add_argument('-c', '--collection',
                        help='The collection to load into',
                        type=str)

    parser.add_argument('-o', '--org',
                        help='The org to load into',
                        type=str,
                        default='usergrid')

    parser.add_argument('-a', '--app',
                        help='The org to load into',
                        type=str,
                        default='supermarkets')

    my_args = parser.parse_args(sys.argv[1:])

    logger.info(str(my_args))

    return vars(my_args)


def get_collection_from_filename(file_path):
    parts = file_path.split('/')

    filename = parts[len(parts)-1]
    parts = filename.split('_')

    collection_name = parts[1]

    return collection_name


class FileProcessor(Process):
    def __init__(self, mod=1, num=1, **kwargs):
        super(FileProcessor, self).__init__()

        self.filename = kwargs.get('filename')
        self.collection_name = kwargs.get('collection')
        self.org_name = kwargs.get('org', 'missing-org')
        self.app_name = kwargs.get('app', 'missing-app')
        self.config = kwargs.get('config')

        with open(self.config, 'r') as f:
            self.config = json.load(f)
            f.close()

        self.sqs_conn = boto.sqs.connect_to_region(**self.config['sqs'])

        if kwargs.get('queue_name'):
            self.queue_name = kwargs.get('queue_name')
        else:
            # self.queue_name = kwargs.get('collection')
            self.queue_name = kwargs.get('filename').replace('.', '_').replace('/', '_')

            if self.queue_name[0] == '_':
                self.queue_name = self.queue_name[-1:]

        self.error_queue_name = self.queue_name + '-errors'
        self.parse_error_queue_name = self.queue_name + '-parse-errors'
        self.parse_error_queue = None
        self.message_queue = None
        self.message_queue_repair = None

        self.mod = mod
        self.num = num

    def run(self):
        start_time = datetime.datetime.utcnow()

        counter = 0

        logger.info('Connecting to queue {0}'.format(self.queue_name))
        queue = attach_queue(self.sqs_conn, self.queue_name)
        repair_queue = attach_queue(self.sqs_conn, self.queue_name + '-repair')

        if not queue:
            queue = self.sqs_conn.create_queue(self.queue_name)

            if not queue:
                logger.info('unable to bind to queue %s' % self.queue_name)
                return

        try:
            counter = 0
            batch = []

            logger.info('Opening file {0}'.format(self.filename))

            for line in smart_open.smart_open(self.filename):

                counter += 1

                if counter % 1000 == 1:
                    logger.info(counter)

                line = line.rstrip()

                if counter > 1 and (not line or line == ''):
                    # logger.info('no line!')
                    break

                if len(line) > 0 and not line[0] == '{':
                    self.report_exception(line, 'line does not start with an open-bracket')
                    continue

                if counter % self.mod != self.num:
                    continue

                entity = None

                try:
                    entity = json.loads(line)

                except:
                    self.report_exception(line, traceback.format_exc())
                    continue

                message = {
                    'org': self.org_name,
                    'app': self.app_name,
                    'collection': self.collection_name,
                    'entity': entity
                }

                if not SKIP_SQS:

                    if len(str(batch)) + len(str(message)) < MAX_SIZE:
                        batch.append((str(uuid.uuid1()), json.dumps(message), 0))

                    else:
                        logger.info('Write_batch (BATCH_SIZE=%s) size=%s counter=%s' % (
                            BATCH_SIZE, len(batch), counter))
                        queue.write_batch(batch)
                        repair_queue.write_batch(batch)
                        batch = []
                        batch.append((str(uuid.uuid1()), json.dumps(message), 0))

                    if len(batch) >= BATCH_SIZE:
                        logger.info('Write_batch (BATCH_SIZE=%s) size=%s counter=%s' % (
                            BATCH_SIZE, len(batch), counter))
                        queue.write_batch(batch)
                        repair_queue.write_batch(batch)
                        batch = []

            if not SKIP_SQS:
                if len(batch) > 0:
                    logger.info(
                        'Write_batch FINAL (BATCH_SIZE=%s) size=%s counter=%s' % (BATCH_SIZE, len(batch), counter))
                    queue.write_batch(batch)
                    repair_queue.write_batch(batch)

        except KeyboardInterrupt:
            logger.warn('terminating...')

        stop_time = datetime.datetime.utcnow()
        duration = stop_time - start_time

        logger.info('done! duration %s(s) count: %s' % (duration, counter))

    def report_exception(self, line, message):

        message = {
            'line': line,
            'exception': message
        }

        m = RawMessage()
        m.set_body(json.dumps(message, 2))

        if self.parse_error_queue is None:
            self.parse_error_queue = attach_queue(self.sqs_conn, self.parse_error_queue_name)

        self.parse_error_queue.write(m)


def main():
    init_logging(stdout_enabled=True)
    args = parse_args()
    threads = args.get('threads')

    if args.get('collection') is None:
        args['collection'] = get_collection_from_filename(args.get('filename'))

    processes = []

    try:
        processes = [FileProcessor(mod=threads, num=x, **args) for x in xrange(threads)]
        [p.start() for p in processes]

    except KeyboardInterrupt:
        logger.info('Keyboard Interrupt.  Terminating...')
        [p.terminate() for p in processes]


if __name__ == '__main__':
    main()
