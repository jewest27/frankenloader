# !/usr/bin/env python

import os
import sys

# minimum required version is 2.6; py3k not supported yet
if not ((2, 6) <= sys.version_info < (3, 0)):
    raise ImportError("smart_open requires 2.6 <= python < 3")

from setuptools import setup, find_packages


def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


setup(
    name='smart_open',
    version='1.1.0',
    description='Utils for working with Usergrid Downloads',
    packages=find_packages(),

    author=u'Jeffrey West',
    author_email='west.jeff@gmail.com',
    maintainer=u'Jeffrey West',
    maintainer_email='west.jeff@gmail.com',

    url='usergrid.incubator.apache.org',

    license='Apache',
    platforms='any',

    install_requires=[
        'boto >= 2.0',
        'bz2file',
        'requests',
        'smart_open'
    ]
)
