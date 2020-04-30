#!/usr/bin/env python3

import argparse
import io
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from tempfile import NamedTemporaryFile, mkstemp

import singer
from joblib import Parallel, delayed, parallel_backend
from jsonschema import Draft4Validator, FormatChecker

from target_postgres.db_sync import DbSync


LOGGER = singer.get_logger('target_postgres')


def float_to_decimal(value):
    """Walk the given data structure and turn all instances of float into
    double."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [float_to_decimal(child) for child in value]
    if isinstance(value, dict):
        return {k: float_to_decimal(v) for k, v in value.items()}
    return value


def add_metadata_columns_to_schema(schema_message):
    """Metadata _sdc columns according to the stitch documentation at
    https://www.stitchdata.com/docs/data-structure/integration-schemas#sdc-columns

    Metadata columns gives information about data injections
    """
    extended_schema_message = schema_message
    extended_schema_message['schema']['properties']['_sdc_extracted_at'] = {'type': ['null', 'string'],
                                                                            'format': 'date-time'}
    extended_schema_message['schema']['properties']['_sdc_batched_at'] = {'type': ['null', 'string'],
                                                                          'format': 'date-time'}
    extended_schema_message['schema']['properties']['_sdc_deleted_at'] = {'type': ['null', 'string']}

    return extended_schema_message


def add_metadata_values_to_record(record_message):
    """Populate metadata _sdc columns from incoming record message
    The location of the required attributes are fixed in the stream
    """
    extended_record = record_message['record']
    extended_record['_sdc_extracted_at'] = record_message.get('time_extracted')
    extended_record['_sdc_batched_at'] = datetime.now().isoformat()
    extended_record['_sdc_deleted_at'] = record_message.get('record', {}).get('_sdc_deleted_at')

    return extended_record


def emit_state(state):
    """Emit state message to standard output then it can be
    consumed by other components"""
    if state is not None:
        line = json.dumps(state)
        LOGGER.debug('Emitting state %s', line)
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()


# pylint: disable=too-many-locals,too-many-branches,too-many-statements,invalid-name,consider-iterating-dictionary
def persist_lines(config, lines):
    """Read singer messages and process them line by line"""
    state = None
    schemas = {}
    key_properties = {}
    validators = {}
    records_to_load = {}
    csv_files_to_load = {}
    row_count = {}
    stream_to_sync = {}
    batch_size_rows = config.get('batch_size_rows', 100000)

    # Loop over lines from stdin
    for line in lines:
        try:
            o = json.loads(line)
        except json.decoder.JSONDecodeError:
            LOGGER.error('Unable to parse:\n%s', line)
            raise

        if 'type' not in o:
            raise Exception("Line is missing required key 'type': {}".format(line))
        t = o['type']

        if t == 'RECORD':
            if 'stream' not in o:
                raise Exception("Line is missing required key 'stream': {}".format(line))
            if o['stream'] not in schemas:
                raise Exception(
                    "A record for stream {} was encountered before a corresponding schema".format(o['stream']))

            # Get schema for this record's stream
            stream = o['stream']

            # Validate record
            try:
                validators[stream].validate(float_to_decimal(o['record']))
            except Exception as ex:
                if type(ex).__name__ == "InvalidOperation":
                    LOGGER.error("Data validation failed and cannot load to destination. RECORD: %s\n'multipleOf' "
                                 "validations that allows long precisions are not supported (i.e. with 15 digits or"
                                 "more). Try removing 'multipleOf' methods from JSON schema.", o['record'])
                    raise ex

            primary_key_string = stream_to_sync[stream].record_primary_key_string(o['record'])
            if not primary_key_string:
                primary_key_string = 'RID-{}'.format(row_count[stream])

            if stream not in records_to_load:
                records_to_load[stream] = {}

            if config.get('add_metadata_columns') or config.get('hard_delete'):
                records_to_load[stream][primary_key_string] = add_metadata_values_to_record(o)
            else:
                records_to_load[stream][primary_key_string] = o['record']

            row_count[stream] = len(records_to_load[stream])

            if row_count[stream] >= batch_size_rows:
                flush_records(stream, records_to_load[stream], row_count[stream], stream_to_sync[stream])
                row_count[stream] = 0
                records_to_load[stream] = {}

            state = None
        elif t == 'STATE':
            LOGGER.debug('Setting state to %s', o['value'])
            state = o['value']
        elif t == 'SCHEMA':
            if 'stream' not in o:
                raise Exception("Line is missing required key 'stream': {}".format(line))
            stream = o['stream']

            schemas[stream] = o
            schema = float_to_decimal(o['schema'])
            validators[stream] = Draft4Validator(schema, format_checker=FormatChecker())

            # flush records from previous stream SCHEMA
            if row_count.get(stream, 0) > 0:
                flush_records(stream, records_to_load[stream], row_count[stream], stream_to_sync[stream])

            # key_properties key must be available in the SCHEMA message.
            if 'key_properties' not in o:
                raise Exception("key_properties field is required")

            # Log based and Incremental replications on tables with no Primary Key
            # cause duplicates when merging UPDATE events.
            # Stop loading data by default if no Primary Key.
            #
            # If you want to load tables with no Primary Key:
            #  1) Set ` 'primary_key_required': false ` in the target-postgres config.json
            #  or
            #  2) Use fastsync [postgres-to-snowflake, mysql-to-snowflake, etc.]
            if config.get('primary_key_required', True) and len(o['key_properties']) == 0:
                LOGGER.critical("Primary key is set to mandatory but not defined in the [%s] stream", stream)
                raise Exception("key_properties field is required")

            key_properties[stream] = o['key_properties']

            if config.get('add_metadata_columns') or config.get('hard_delete'):
                stream_to_sync[stream] = DbSync(config, add_metadata_columns_to_schema(o))
            else:
                stream_to_sync[stream] = DbSync(config, o)

            stream_to_sync[stream].create_schema_if_not_exists()
            stream_to_sync[stream].sync_table()
            row_count[stream] = 0
            csv_files_to_load[stream] = NamedTemporaryFile(mode='w+b')
        elif t == 'ACTIVATE_VERSION':
            LOGGER.debug('ACTIVATE_VERSION message')
        else:
            raise Exception("Unknown message type {} in message {}"
                            .format(o['type'], o))


    # Single-host, thread-based parallelism
    with parallel_backend('threading', n_jobs=-1):
        Parallel()(delayed(load_stream_batch)(
            stream=stream,
            records_to_load=records_to_load[stream],
            row_count=row_count[stream],
            db_sync=stream_to_sync[stream],
            delete_rows=config.get('hard_delete'),
            temp_dir=config.get('temp_dir')
        ) for stream in records_to_load.keys())

    return state


# pylint: disable=too-many-arguments
def load_stream_batch(stream, records_to_load, row_count, db_sync, delete_rows=False, temp_dir=None):
    """Load a batch of records and do post load operations, like creating
    or deleting rows"""
    # Load into snowflake
    if row_count > 0:
        flush_records(stream, records_to_load, row_count, db_sync, temp_dir)

    # Load finished, create indices if required
    db_sync.create_indices(stream)

    # Delete soft-deleted, flagged rows - where _sdc_deleted at is not null
    if delete_rows:
        db_sync.delete_rows(stream)


# pylint: disable=unused-argument
def flush_records(stream, records_to_load, row_count, db_sync, temp_dir=None):
    """Take a list of records and load into database"""
    if temp_dir:
        temp_dir = os.path.expanduser(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

    csv_fd, csv_file = mkstemp(suffix='.csv', prefix=f'{stream}_', dir=temp_dir)
    with open(csv_fd, 'w+b') as f:
        for record in records_to_load.values():
            csv_line = db_sync.record_to_csv_line(record)
            f.write(bytes(csv_line + '\n', 'UTF-8'))

        # Seek to the beginning of the file and load
        f.seek(0)
        db_sync.load_csv(f, row_count)

    # Delete temp file
    os.remove(csv_file)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', help='Config file')
    args = parser.parse_args()

    if args.config:
        with open(args.config) as config_input:
            config = json.load(config_input)
    else:
        config = {}

    # Consume singer messages
    singer_messages = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    state = persist_lines(config, singer_messages)

    emit_state(state)
    LOGGER.debug("Exiting normally")


if __name__ == '__main__':
    main()
