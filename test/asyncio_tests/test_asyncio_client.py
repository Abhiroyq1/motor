# Copyright 2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test AsyncIOMotorClient."""

import asyncio
import os
import unittest
import warnings
from unittest import SkipTest

import bson
import pymongo
from bson import CodecOptions
from pymongo import ReadPreference, WriteConcern
from pymongo.errors import ConnectionFailure, OperationFailure

from motor import motor_asyncio

import test
from test.asyncio_tests import (asyncio_test,
                                AsyncIOTestCase,
                                AsyncIOMockServerTestCase,
                                remove_all_users)
from test.test_environment import db_user, db_password, env
from test.utils import get_primary_pool


class TestAsyncIOClient(AsyncIOTestCase):
    def test_host_port_deprecated(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with self.assertRaises(DeprecationWarning):
                self.cx.host

            with self.assertRaises(DeprecationWarning):
                self.cx.port

    def test_document_class_deprecated(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with self.assertRaises(DeprecationWarning):
                self.cx.document_class

            with self.assertRaises(DeprecationWarning):
                # Setting the property is deprecated, too.
                self.cx.document_class = bson.SON

    @asyncio_test
    def test_client_lazy_connect(self):
        yield from self.db.test_client_lazy_connect.remove()

        # Create client without connecting; connect on demand.
        cx = self.asyncio_client()
        collection = cx.motor_test.test_client_lazy_connect
        future0 = collection.insert({'foo': 'bar'})
        future1 = collection.insert({'foo': 'bar'})
        yield from asyncio.gather(future0, future1, loop=self.loop)
        resp = yield from collection.find({'foo': 'bar'}).count()
        self.assertEqual(2, resp)
        cx.close()

    @asyncio_test
    def test_close(self):
        cx = self.asyncio_client()
        cx.close()
        self.assertEqual(None, get_primary_pool(cx))

    @asyncio_test
    def test_unix_socket(self):
        mongodb_socket = '/tmp/mongodb-%d.sock' % env.port

        if not os.access(mongodb_socket, os.R_OK):
            raise SkipTest("Socket file is not accessible")

        encoded_socket = '%2Ftmp%2Fmongodb-' + str(env.port) + '.sock'
        uri = 'mongodb://%s' % encoded_socket
        client = self.asyncio_client(uri)
        collection = client.motor_test.test

        if test.env.auth:
            yield from client.admin.authenticate(db_user, db_password)
        yield from collection.insert({"dummy": "object"})

        # Confirm it fails with a missing socket.
        client = motor_asyncio.AsyncIOMotorClient(
            "mongodb://%2Ftmp%2Fnon-existent.sock", io_loop=self.loop,
            serverSelectionTimeoutMS=100)

        with self.assertRaises(ConnectionFailure):
            yield from client.admin.command('ismaster')
        client.close()

    def test_database_named_delegate(self):
        self.assertTrue(
            isinstance(self.cx.delegate, pymongo.mongo_client.MongoClient))
        self.assertTrue(isinstance(self.cx['delegate'],
                                   motor_asyncio.AsyncIOMotorDatabase))

    @asyncio_test
    def test_reconnect_in_case_connection_closed_by_mongo(self):
        cx = self.asyncio_client(maxPoolSize=1)
        yield from cx.admin.command('ping')

        # close motor_socket, we imitate that connection to mongo server
        # lost, as result we should have AutoReconnect instead of
        # IncompleteReadError
        pool = get_primary_pool(cx)
        socket = pool.sockets.pop()
        socket.sock.close()
        pool.sockets.add(socket)

        with self.assertRaises(pymongo.errors.AutoReconnect):
            yield from cx.motor_test.test_collection.find_one()

    @asyncio_test
    def test_connection_failure(self):
        # Assuming there isn't anything actually running on this port
        client = motor_asyncio.AsyncIOMotorClient('localhost', 8765,
                                                  serverSelectionTimeoutMS=10,
                                                  io_loop=self.loop)

        with self.assertRaises(ConnectionFailure):
            yield from client.admin.command('ismaster')

    @asyncio_test(timeout=30)
    def test_connection_timeout(self):
        # Motor merely tries to time out a connection attempt within the
        # specified duration; DNS lookup in particular isn't charged against
        # the timeout. So don't measure how long this takes.
        client = motor_asyncio.AsyncIOMotorClient(
            'example.com', port=12345,
            serverSelectionTimeoutMS=1, io_loop=self.loop)

        with self.assertRaises(ConnectionFailure):
            yield from client.admin.command('ismaster')

    @asyncio_test
    def test_max_pool_size_validation(self):
        with self.assertRaises(ValueError):
            motor_asyncio.AsyncIOMotorClient(maxPoolSize=-1,
                                             io_loop=self.loop)

        with self.assertRaises(ValueError):
            motor_asyncio.AsyncIOMotorClient(maxPoolSize='foo',
                                             io_loop=self.loop)

        cx = self.asyncio_client(maxPoolSize=100)
        self.assertEqual(cx.max_pool_size, 100)
        cx.close()

    @asyncio_test(timeout=60)
    def test_high_concurrency(self):
        return
        yield from self.make_test_data()

        concurrency = 25
        cx = self.asyncio_client(maxPoolSize=concurrency)
        expected_finds = 200 * concurrency
        n_inserts = 25

        collection = cx.motor_test.test_collection
        insert_collection = cx.motor_test.insert_collection
        yield from insert_collection.remove()

        ndocs = 0
        insert_future = asyncio.Future(loop=self.loop)

        @asyncio.coroutine
        def find():
            nonlocal ndocs
            cursor = collection.find()
            while (yield from cursor.fetch_next):
                cursor.next_object()
                ndocs += 1

                # Half-way through, start an insert loop
                if ndocs == expected_finds / 2:
                    asyncio.Task(insert(), loop=self.loop)

        @asyncio.coroutine
        def insert():
            for i in range(n_inserts):
                yield from insert_collection.insert({'s': hex(i)})

            insert_future.set_result(None)  # Finished

        yield from asyncio.gather(*[find() for _ in range(concurrency)],
                                  loop=self.loop)
        yield from insert_future
        self.assertEqual(expected_finds, ndocs)
        self.assertEqual(n_inserts, (yield from insert_collection.count()))
        yield from collection.remove()

    @asyncio_test(timeout=30)
    def test_drop_database(self):
        # Make sure we can pass an AsyncIOMotorDatabase instance
        # to drop_database
        db = self.cx.test_drop_database
        yield from db.test_collection.insert({})
        names = yield from self.cx.database_names()
        self.assertTrue('test_drop_database' in names)
        yield from self.cx.drop_database(db)
        names = yield from self.cx.database_names()
        self.assertFalse('test_drop_database' in names)

    @asyncio_test
    def test_auth_from_uri(self):
        if not test.env.auth:
            raise SkipTest('Authentication is not enabled on server')

        # self.db is logged in as root.
        yield from remove_all_users(self.db)
        db = self.db
        try:
            yield from db.add_user(
                'mike', 'password',
                roles=['userAdmin', 'readWrite'])

            client = motor_asyncio.AsyncIOMotorClient(
                'mongodb://u:pass@%s:%d' % (env.host, env.port),
                io_loop=self.loop)

            # ismaster doesn't throw auth errors.
            yield from client.admin.command('ismaster')

            with self.assertRaises(OperationFailure):
                yield from client.db.collection.find_one()

            client = motor_asyncio.AsyncIOMotorClient(
                'mongodb://mike:password@%s:%d/%s' %
                (env.host, env.port, db.name),
                io_loop=self.loop)

            yield from client[db.name].collection.find_one()
        finally:
            yield from db.remove_user('mike')

    @asyncio_test
    def test_socketKeepAlive(self):
        # Connect.
        yield from self.cx.server_info()
        ka = get_primary_pool(self.cx).opts.socket_keepalive
        self.assertFalse(ka)

        client = self.asyncio_client(socketKeepAlive=True)
        yield from client.server_info()
        ka = get_primary_pool(client).opts.socket_keepalive
        self.assertTrue(ka)

    def test_get_database(self):
        codec_options = CodecOptions(tz_aware=True)
        write_concern = WriteConcern(w=2, j=True)
        db = self.cx.get_database(
            'foo', codec_options, ReadPreference.SECONDARY, write_concern)

        assert isinstance(db, motor_asyncio.AsyncIOMotorDatabase)
        self.assertEqual('foo', db.name)
        self.assertEqual(codec_options, db.codec_options)
        self.assertEqual(ReadPreference.SECONDARY, db.read_preference)
        self.assertEqual(write_concern, db.write_concern)


class TestAsyncIOClientTimeout(AsyncIOMockServerTestCase):
    @asyncio_test
    def test_timeout(self):
        server = self.server(auto_ismaster=True)
        client = motor_asyncio.AsyncIOMotorClient(server.uri,
                                                  socketTimeoutMS=100,
                                                  io_loop=self.loop)

        with self.assertRaises(pymongo.errors.AutoReconnect) as context:
            yield from client.motor_test.test_collection.find_one()

        self.assertIn('timed out', str(context.exception))
        client.close()


if __name__ == '__main__':
    unittest.main()
