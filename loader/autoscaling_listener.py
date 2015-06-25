from Queue import Empty
import base64
from collections import defaultdict
import json
from multiprocessing import Process, JoinableQueue, Queue
import os
import traceback
import sys
import datetime
import time
import logging
from logging.handlers import RotatingFileHandler

import argparse
import boto
from boto.sqs.message import RawMessage
import requests


MAX_MESSAGES = 10
MAX_THREADS = 64

logger = logging.getLogger('QueueListener')


def init_logging(stdout_enabled=True):
    root_logger = logging.getLogger()
    log_file_name = './listener.log'
    log_formatter = logging.Formatter(fmt='%(asctime)s | %(name)s | %(processName)s | %(levelname)s | %(message)s',
                                      datefmt='%m/%d/%Y %I:%M:%S %p')

    rotating_file = logging.handlers.RotatingFileHandler(filename=log_file_name,
                                                         mode='a',
                                                         maxBytes=104857600,
                                                         backupCount=10)
    rotating_file.setFormatter(log_formatter)
    rotating_file.setLevel(logging.INFO)

    root_logger.addHandler(rotating_file)
    root_logger.setLevel(logging.INFO)

    logging.getLogger('boto').setLevel(logging.ERROR)

    if stdout_enabled:
        stdout_logger = logging.StreamHandler(sys.stdout)
        stdout_logger.setFormatter(log_formatter)
        stdout_logger.setLevel(logging.INFO)
        root_logger.addHandler(stdout_logger)


def total_seconds(td):
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10 ** 6) / 10 ** 6


def total_milliseconds(td):
    return (td.microseconds + td.seconds * 1000000) / 1000


def attach_queue(sqs_conn, queue_name):
    try:
        queue = sqs_conn.get_queue(queue_name)

        if not queue:
            queue = sqs_conn.create_queue(queue_name)

        return queue

    except Exception, e:
        logger.info(traceback.format_exc())
        raise e


def get_message_body(sqs_message):
    message_body = sqs_message.get_body()

    try:
        message = json.loads(message_body)
    except ValueError:
        logger.warning('No JSON: %s' % message_body)
        try:
            base64_decoded = base64.b64decode(message_body)
            logger.warning('Base64 Decoded: %s' % base64_decoded)
            message = json.loads(base64_decoded)
        except:
            return None

    return message


class QueueListener(Process):
    def __init__(self, config, delete_queue, control_queue, **kwargs):
        super(QueueListener, self).__init__()
        self.done = False
        self.control_queue = control_queue

        self.session_usergrid = requests.Session()
        self.session_elastic = requests.Session()
        self.sqs_config = config.get('sqs')
        self.delete_queue = delete_queue
        self.base_url = config.get('ug_base_url')
        self.usergrid_headers = {"Content-Type": "application/json"}
        self.credential_map = config.get('credential_map')
        self.queue_name = config.get('queue_name')

        elastic_config = config.get('elastic', {})
        self.check_name_exists = elastic_config.get('check_name_exists', False)
        self.check_queue_depth = elastic_config.get('check_queue_depth', False)
        self.queue_full_threshold = elastic_config.get('queue_threshold', 50000)
        self.queue_full_wait_time = elastic_config.get('queue_wait_time', 1000)
        self.es_index = elastic_config.get('index_name')
        self.es_url = elastic_config.get('es_url_base')
        self.es_index_url = '%s/%s' % (self.es_url, self.es_index)

        logger.info('connecting to queue %s...' % self.queue_name)

        self.sqs_conn = boto.sqs.connect_to_region(**self.sqs_config)
        self.message_queue = self.sqs_conn.get_queue(self.queue_name)

        if self.message_queue is None:
            logger.info('Unable to connect to queue: %s' % self.queue_name)
            exit(1)

        self.error_queue_name = self.queue_name + '-error'

        if self.error_queue_name[-12:] == '-error-error':
            self.error_queue_name = self.queue_name

        self.exception_queue_name = self.queue_name + '-exception'
        self.parse_error_queue_name = self.queue_name + '-parse-errors'
        self.malformed_message_queue_name = self.queue_name + '-malformed'
        self.malformed_message_queue_name = self.queue_name + '-reprocess'

        self.error_queue = None
        self.exception_queue = None
        self.parse_error_queue = None
        self.malformed_message_queue = None

    def check_exists(self, collection_name, entity_name):
        if True:
            return False

        try:

            url = '%s/_search' % self.es_index_url

            request = {
                "query": {
                    "filtered": {
                        "filter": {
                            "terms": {
                                "su_name": [entity_name]
                            }
                        }
                    }
                }
            }

            r = self.session_elastic.get(url=url, data=json.dumps(request))

            # logger.info( 'ES: GET (%s) in %s: %s' % (r.status_code, total_milliseconds(r.elapsed), url))

            if not r.status_code == 200:
                logger.error('Unable to check if name exists!!')
                return False

            response = r.json()

            count = response.get('hits', {}).get('total', 0)

            return count >= 1

        except:
            logger.error(traceback.format_exc())
            return False

    def persist_entity(self, oac_tuple, the_entity, method='PUT'):
        # logger.info( 'Loading entity to Usergrid...')

        try:
            if method == 'PUT':
                url = self.get_entity_url(oac_tuple, the_entity)

                r = self.session_usergrid.put(url=url,
                                              data=json.dumps(the_entity),
                                              headers=self.usergrid_headers)
            else:
                url = self.get_collection_url(oac_tuple)

                r = self.session_usergrid.post(url=url,
                                               data=json.dumps(the_entity),
                                               headers=self.usergrid_headers)

            response_time = total_milliseconds(r.elapsed)

            if r.status_code != 200:
                logger.info('%s (%s) in %s: %s' % (method, r.status_code, response_time, url))

            report_error = False
            message = ''

            if r.status_code != 200:
                report_error = True

                try:
                    message = r.json()

                except ValueError, e:
                    message = r.text

            else:
                try:
                    response = r.json()

                    if len(response.get('entities')) != 1:
                        message = 'Entity not returned after %s' % method
                        report_error = True

                except ValueError, e:
                    report_error = True
                    message = 'Exception (%s) getting JSON from response!' % e
                    logger.info(message)
                    logger.info('response: %s' % r.text)

                except Exception, e:
                    report_error = True
                    message = 'Exception (%s) getting JSON from response!' % e
                    logger.info(message)
                    logger.info('response: %s' % r.text)

            if report_error:
                self.post_error(oac_tuple, the_entity, message)

            return response_time

        except Exception, e:
            self.post_error(oac_tuple, the_entity, 'Error on %s: %s' % (method, traceback.format_exc()))
            logger.info(traceback.format_exc())
            raise e

    def put_entities_to_usergrid(self, oac_tuple, entity_wrappers):

        try:
            collection_url = self.get_collection_url(oac_tuple)

            logger.info('Loading %s to collection %s URL: %s' % (len(entity_wrappers), oac_tuple, collection_url))

            for entity_wrapper in entity_wrappers:

                r = requests.put(url=collection_url,
                                 data=json.dumps(entity_wrapper),
                                 headers=self.usergrid_headers)

                if r.status_code != 200:
                    self.post_error(oac_tuple, entity_wrapper, r.json())

        except Exception, e:
            self.post_error(oac_tuple, entity_wrappers, traceback.format_exc())
            logger.info(traceback.format_exc())
            raise e

    def post_entities_to_usergrid(self, oac_tuple, the_entities):

        try:
            collection_url = self.get_collection_url(oac_tuple)

            # logger.info( 'Loading %s to collection %s URL: %s' % (len(the_entities), oac_tuple, collection_url))

            r = requests.post(url=collection_url,
                              data=json.dumps(the_entities),
                              headers=self.usergrid_headers)

            response = r.json()

            if r.status_code != 200:
                self.post_error_array(oac_tuple, the_entities, r.json())

            elif response is not None:
                if len(response.get('entities')) != len(the_entities):
                    logger.info('Wrong Entity Count from post!  %s not %s' % (
                        len(response.get('entities')), len(the_entities)))

                    entity_map = {}

                    for response_entity in response.get('entities'):
                        entity_map[response_entity.get('name')] = response_entity

                    if the_entities:
                        for entity in the_entities:
                            if entity and entity.get('name') not in entity_map:
                                self.persist_entity(oac_tuple, entity, method='POST')
            else:
                raise Exception('Unexpected - JW')

        except Exception, e:
            self.post_error(oac_tuple, the_entities, e)
            logger.info(traceback.format_exc())
            raise e

    def check_queues_and_wait(self):
        if True:
            return

        should_continue = False

        while not should_continue:
            url = '%s/_cat/thread_pool?v=&h=iq' % self.es_url
            # logger.info( url)

            response = requests.get(url)

            # logger.info( response.text)

            lines = response.text.split('\n')

            total_queued = 0L
            line_counter = 0

            for line in lines:

                line_counter += 1

                if line_counter == 1:
                    continue

                if line == '':
                    continue

                try:
                    line = line.rstrip().lstrip()
                    # logger.info( '[%s]' % line)
                    total_queued += long(line)
                except:
                    continue

            if total_queued > self.queue_full_threshold:
                logger.warning('MAX QUEUED (%s) MET.  Sleeping...' % total_queued)
                time.sleep(self.queue_full_wait_time)
                should_continue = False
            else:
                if total_queued > 0:
                    logger.info('ES INDEX Nodes: %s Queue Size: %s' % (len(lines) - 1, total_queued))

                should_continue = True

    def run(self):
        message_counter = 0L
        start_time = datetime.datetime.utcnow()
        sqs_iterations = 0
        sqs_total_response = 0

        total_response_time = 0

        zero_counter = 0
        under_max_counter = 0

        while not self.done:

            self.check_queues_and_wait()

            sqs_start_time = datetime.datetime.utcnow()

            sqs_messages = self.message_queue.get_messages(num_messages=MAX_MESSAGES,
                                                           wait_time_seconds=10)

            self.control_queue.put(
                {
                    'name': self.name,
                    'count': len(sqs_messages),
                    'was_max': len(sqs_messages) == MAX_MESSAGES
                })

            sqs_stop_time = datetime.datetime.utcnow()
            sqs_iterations += 1
            sqs_response_time = total_milliseconds(sqs_stop_time - sqs_start_time)
            sqs_total_response += sqs_response_time

            if len(sqs_messages) < MAX_MESSAGES:
                under_max_counter += 1

                if under_max_counter >= 10:
                    self.done = True

                if len(sqs_messages) == 0:
                    zero_counter += 1

                    if zero_counter >= 10:
                        self.done = True
                        break
            else:
                zero_counter = 0
                under_max_counter = 0

            if message_counter > 0:
                logger.info(
                    'Received Messages! queue: %s | count: %s | SQS response time: %sms  | SQS avg response time: %sms | BaaS Avg Response %sms' % (
                        self.queue_name,
                        len(sqs_messages), sqs_response_time,
                        (sqs_total_response / sqs_iterations),
                        (total_response_time / message_counter)))

            else:
                logger.info('No messages returned! queue: %s | zero_counter: %s | under_max_counter: %s' % (
                    self.queue_name, zero_counter, under_max_counter))

            for sqs_message in sqs_messages:

                message_counter += 1
                # logger.info( 'Counter: %s' % message_counter)

                try:
                    message = get_message_body(sqs_message)

                    if message is None:
                        self.delete_queue.put(sqs_message)
                        continue

                    if not 'org' in message or not 'app' in message or not 'collection' in message or not 'entity' in message:
                        self.post_malformed_message_error(sqs_message)
                        self.delete_queue.put(sqs_message)
                        continue

                    org = message.get('org')
                    app = message.get('app')
                    collection = message.get('collection')
                    entity = message.get('entity')

                    if 'name' in entity:
                        if not self.check_exists(collection, entity.get('name')):

                            # logger.info( json.dumps(entity, indent=2))

                            response_time = self.persist_entity(
                                oac_tuple={'org': org, 'app': app, 'collection': collection},
                                the_entity=entity,
                                method='PUT')
                        else:
                            # logger.info( '%s:%s exists!' % (collection, entity.get('name')))
                            response_time = 0
                            # logger.info( 'PUT response time: %s' % response_time)
                    else:
                        response_time = self.persist_entity(
                            oac_tuple={'org': org, 'app': app, 'collection': collection},
                            the_entity=entity,
                            method='POST')

                        # logger.info( 'POST response time: %s' % response_time)

                    total_response_time += response_time

                    self.delete_queue.put(sqs_message)

                except Exception, e:
                    logger.error(e)
                    logger.error(traceback.format_exc())

            if len(sqs_messages) == 0:
                time.sleep(30)

            # after loop
            now_time = datetime.datetime.utcnow()
            duration_delta = now_time - start_time
            duration = total_seconds(duration_delta)

            if duration > 0 and message_counter % 100 == 0:
                logger.info('counter: %s eps: %s' % (message_counter, 1.0 * message_counter / duration))

        logger.warning('This thread is done!')

    def post_error(self, oac_tuple, the_entity, response_message):
        logger.error('Response Message: ' + str(response_message))
        message = {
            'org': oac_tuple.get('org'),
            'app': oac_tuple.get('app'),
            'collection': oac_tuple.get('collection'),

            'response/message': response_message,

            'entity': the_entity
        }

        m = RawMessage()
        m.set_body(json.dumps(message, 2))

        if self.error_queue is None:
            self.error_queue = attach_queue(self.sqs_conn, self.error_queue_name)

        self.error_queue.write(m)

    def post_exception(self, oac_tuple, the_entity, traceback_exception):
        message = {
            'org': oac_tuple.get('org'),
            'app': oac_tuple.get('app'),
            'collection': oac_tuple.get('collection'),

            'exception': str(traceback_exception),

            'entity': the_entity
        }

        m = RawMessage()
        m.set_body(json.dumps(message, 2))

        if self.exception_queue is None:
            self.exception_queue = attach_queue(self.sqs_conn, self.exception_queue_name)

        self.exception_queue.write(m)

    def post_parse_error(self, entity_line, traceback_exception):
        message = {
            'line': entity_line,
            'exception': str(traceback_exception)
        }

        m = RawMessage()
        m.set_body(json.dumps(message, 2))

        if self.parse_error_queue is None:
            self.parse_error_queue = attach_queue(self.sqs_conn, self.parse_error_queue_name)

        self.parse_error_queue.write(m)

    def post_error_array(self, oac_tuple, the_entities, response):
        for entity in the_entities:
            self.post_error(oac_tuple, entity, response)

    def post_exception_array(self, oac_tuple, the_entities, response):

        for entity in the_entities:
            self.post_exception(oac_tuple, entity, response)

    def get_collection_url(self, oac_tuple):
        auth_params = self.get_org_credentials(org=oac_tuple.get('org'))

        params = {
            'org': oac_tuple.get('org'),
            'app': oac_tuple.get('app'),
            'collection': oac_tuple.get('collection'),
            'client_id': auth_params.get('client_id'),
            'client_secret': auth_params.get('client_secret')
        }

        return self.base_url + '/{org}/{app}/{collection}?client_id={client_id}&client_secret={client_secret}'.format(
            **params)

    def get_entity_url(self, oac_tuple, the_entity):
        auth_params = self.get_org_credentials(org=oac_tuple.get('org'))

        params = {
            'org': oac_tuple.get('org'),
            'app': oac_tuple.get('app'),
            'collection': oac_tuple.get('collection'),
            'name': the_entity.get('name'),
            'client_id': auth_params.get('client_id'),
            'client_secret': auth_params.get('client_secret')
        }

        return self.base_url + '/{org}/{app}/{collection}/{name}?client_id={client_id}&client_secret={client_secret}'.format(
            **params)

    def get_org_credentials(self, org):

        if not org in self.credential_map:
            raise Exception('Credentials not found for org=[%s]' % org)

        return self.credential_map.get(org)

    def post_malformed_message_error(self, sqs_message):
        pass


def time_now():
    return unix_time(datetime.datetime.utcnow())


def unix_time(dt):
    epoch = datetime.utcfromtimestamp(0)
    td = dt - epoch

    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10 ** 6) / 10 ** 6


def parse_args():
    parser = argparse.ArgumentParser(description='Apigee BaaS Loader - Publisher')

    parser.add_argument('-q', '--queue_name',
                        help='The queue to load into',
                        type=str,
                        default='entities')

    parser.add_argument('--config',
                        help='The queue to load into',
                        type=str,
                        default='%s/frankenloader.json' % os.getenv("HOME"))

    parser.add_argument('-t', '--threads',
                        help='The number of threads to run',
                        type=int,
                        default=32)

    parser.add_argument('-i', '--increment',
                        help='The number of threads to increment by',
                        type=int,
                        default=8)

    parser.add_argument('-m', '--max_threads',
                        help='The max number of listeners',
                        type=int,
                        default=64)

    my_args = parser.parse_args(sys.argv[1:])

    logger.info(str(my_args))

    return vars(my_args)


class MessageDeleter(Process):
    def __init__(self, sqs_config, queue_name, delete_queue, **kwargs):
        super(MessageDeleter, self).__init__()
        self.queue_name = queue_name
        self.sqs_config = sqs_config
        self.delete_queue = delete_queue

    def run(self):
        logger.info('MessageDeleter connecting to queue %s...' % self.queue_name)
        sqs_conn = boto.sqs.connect_to_region(**self.sqs_config)
        message_queue = sqs_conn.get_queue(self.queue_name)
        counter = 0

        while True:
            try:
                sqs_message = self.delete_queue.get(timeout=120)

                if sqs_message:
                    counter += 1
                    message_queue.delete_message(sqs_message)

                self.delete_queue.task_done()

            except Empty:
                logger.info('Nothing to delete...')
                time.sleep(60)


class ListenerController():
    def __init__(self, config):
        self.delete_queue = JoinableQueue()
        self.control_queue = Queue()
        self.config = config
        self.sqs_config = config.get('sqs')
        self.queue_name = config.get('queue_name')
        self.thread_count = config.get('threads')

    def prune_and_count(self, workers):
        alive_count = 0
        dead_workers = []
        for worker in workers:
            if worker.is_alive():
                alive_count += 1
            else:
                dead_workers.append(worker)
        for dead_worker in dead_workers:
            workers.remove(dead_worker)
        return alive_count

    def run(self):
        delete_threads = [
            MessageDeleter(queue_name=self.queue_name,
                           sqs_config=self.sqs_config,
                           delete_queue=self.delete_queue)
            for x in xrange(4)
        ]

        logger.info('Starting %s worker threads...' % self.thread_count)

        workers = [
            QueueListener(config=self.config,
                          control_queue=self.control_queue,
                          delete_queue=self.delete_queue)
            for x in xrange(self.thread_count)
        ]

        [worker.start() for worker in workers]
        [deleter.start() for deleter in delete_threads]

        keep_going = True

        counters = defaultdict(int)
        max_counter_threshold = 10
        max_counter = 0

        while keep_going:
            try:
                feedback_message = self.control_queue.get(timeout=60)
                logger.info('Workers: %s | max_count %s' % (len(workers), max_counter))

                alive_count = self.prune_and_count(workers)

                if alive_count == 0:
                    logger.warning('All Threads are done...')

                    # sleep 60, then allocate 2 new threads
                    time.sleep(300)

                    workers = [
                        QueueListener(config=self.config,
                                      control_queue=self.control_queue,
                                      delete_queue=self.delete_queue)
                        for x in xrange(1)
                    ]

                else:
                    if feedback_message.get('was_max', False):
                        counters[feedback_message['name']] += 1

                    for counter_name, i in counters.iteritems():
                        if i > max_counter:
                            max_counter = i

                    if max_counter > max_counter_threshold:
                        counters = defaultdict(int)
                        max_counter = 0

                        if len(workers) < config.get('max_threads', MAX_THREADS):
                            logger.info('Allocating new workers!! Pre-Size: %s' % len(workers))
                            worker_increment = self.config.get('increment', 4)

                            if worker_increment > (config.get('max_threads', MAX_THREADS) - len(workers)):
                                worker_increment = (config.get('max_threads', MAX_THREADS) - len(workers))

                            for i in xrange(worker_increment):
                                logger.info('Allocating worker %s of %s' % (i, worker_increment))
                                worker = QueueListener(config=self.config, control_queue=self.control_queue,
                                                       delete_queue=self.delete_queue)
                                worker.start()
                                workers.append(worker)

                            logger.info('New workers allocated!! Post-Size: %s' % len(workers))
                        else:
                            max_counter = 0
                            logger.info('MAX WORKERS met!!!')

            except Empty:
                logger.info('No feedback response - sleeping 10 minutes')
                time.sleep(600)
                alive_count = self.prune_and_count(workers)

                if alive_count <= 0:
                    [worker.terminate() for worker in workers]
                    workers = [
                        QueueListener(config=self.config,
                                      control_queue=self.control_queue,
                                      delete_queue=self.delete_queue)
                        for x in xrange(1)
                    ]
                    [worker.start() for worker in workers]
            except KeyboardInterrupt:
                keep_going = False

        logger.info('terminating...')
        [worker.terminate() for worker in workers]
        [deleter.terminate() for deleter in delete_threads]


if __name__ == '__main__':
    init_logging(True)
    args = parse_args()
    logger.info('Using config file: %s' % args['config'])

    with open(args['config'], 'r') as f:
        config = json.load(f)
        config.update(args)

    if config is None:
        logger.info('Error loading config!')
        exit()

    sqs_config = config.get('sqs')

    controller = ListenerController(config)
    controller.run()
