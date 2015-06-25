import json
import os
import smtplib
import traceback
import sys
import datetime

import argparse
import boto



def parse_args():
    parser = argparse.ArgumentParser(description='S3 Bucket Downloader')

    parser.add_argument('--s3_config',
                        help='The queue to load into',
                        type=str,
                        default=os.path.join(os.environ.get('HOME'), 'keys/s3_wac.json'))

    parser.add_argument('--contains',
                        help='Only download files that contain this string',
                        type=str)

    parser.add_argument('--target',
                        help='The dir to place the downloaded files',
                        default=os.path.join(os.environ.get('HOME'), 'data'))

    parser.add_argument('--bucket',
                        help='The dir to place the downloaded files',
                        default='wac-dev-apigee')

    my_args = parser.parse_args(sys.argv[1:])

    print str(my_args)

    return vars(my_args)


def get_date_keys():
    date_spec = "%Y%m%d"
    today = datetime.datetime.now()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)

    return [today.strftime(date_spec),
            yesterday.strftime(date_spec),
            tomorrow.strftime(date_spec)]


def do_download(strings):
    print 'Using keys: %s' % strings
    args = parse_args()
    print args
    contains = args.get('contains')

    if contains is not None:
        strings.append(contains)

    with open(args.get('s3_config'), 'r') as f:
        s3_config = json.load(f)

    conn = boto.connect_s3(
        s3_config.get('aws_access_key_id'),
        s3_config.get('aws_secret_access_key'))

    bucket = conn.get_bucket(args.get('bucket'))

    key_list = bucket.list()
    downloaded_files = []

    for key in key_list:

        # print str(key)
        keyString = key.key

        path_str = os.path.join(args['target'], keyString)

        if path_str[-1:] == '/':
            try:
                os.makedirs(path_str[-1:])
            except:
                pass

            continue

        # print 'Evaluating key=[%s] path=[%s]' % (keyString, path_str)

        process = False

        if 'wow/' in keyString:
            continue

        for string in strings:
            if string in keyString:
                print 'Found [%s] in Key [%s]: Processing...' % (string, keyString)
                process = True
                break

        if not process:
            # print 'Skipping Key: %s' % keyString
            continue

        levels = keyString.split('/')

        dir_name = os.path.join(args['target'], *levels[:-1])

        if '/' in keyString and not os.path.exists(dir_name):
            print 'Making dirs: %s' % dir_name
            try:
                os.makedirs(dir_name)
            except:
                pass

        if not path_str[-1:] == '/':  # check if file exists locally, if not: download it
            if not os.path.exists(path_str):
                try:
                    print 'Downloading %s to %s' % (keyString, path_str)
                    key.get_contents_to_filename(path_str)
                    downloaded_files.append(path_str)
                except:
                    print traceback.format_exc()
            else:
                pass

    return downloaded_files


keys = get_date_keys()

downloads = do_download(keys)


def notify_downloads(_downloads):
    if len(_downloads) == 0:
        return

    try:
        server = smtplib.SMTP('smtp.gmail.com:587')
        server.ehlo()
        server.starttls()

        files_str = ''

        for file in _downloads:
            files_str += file
            files_str += '\r\n'

        msg = "\r\n".join([
            "From: jwest@apigee.com",
            "To: jwest@apigee.com",
            "Subject: New Files Downloaded",
            "",
            "%s" % files_str
        ])

        server.login('jwest@apigee.com', 'abc123')
        print 'sending mail!'
        server.sendmail('jwest@apigee.com', 'jwest@apigee.com', msg)
        server.close()
        print 'mail sent!'

    except Exception, e:
        print traceback.format_exc()
        print 'unable to send mail'


notify_downloads(downloads)