"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from stream_alert.athena_partition_refresh.main import StreamAlertAthenaClient
from stream_alert.athena_partition_refresh import helpers as athena_helpers
from stream_alert.rule_processor.handler import StreamAlert
from stream_alert_cli.logger import LOGGER_CLI
from stream_alert_cli.helpers import continue_prompt
from stream_alert_cli.terraform import _common as terraform_cli_helpers

# How to map log schema types to athena/hive schema types
SCHEMA_TYPE_MAPPING = {
    'string': 'string',
    'integer': 'bigint',
    'boolean': 'boolean',
    'float': 'decimal',
    dict: 'map<string, string>',
    list: 'array<string>'
}


def create_database(athena_client):
    """Create the 'streamalert' Athena database

    Args:
        athena_client (boto3.client): Instantiated CLI AthenaClient
    """
    if athena_client.check_database_exists():
        LOGGER_CLI.info('The \'streamalert\' database already exists, nothing to do')
        return

    create_db_success, create_db_result = athena_client.run_athena_query(
        query='CREATE DATABASE streamalert')

    if create_db_success and create_db_result['ResultSet'].get('Rows'):
        LOGGER_CLI.info('streamalert database successfully created!')
        LOGGER_CLI.info('results: %s', create_db_result['ResultSet']['Rows'])


def rebuild_partitions(athena_client, options, config):
    """Rebuild an Athena table's partitions

    Steps:
      - Get the list of current partitions
      - Destroy existing table
      - Re-create tables
      - Re-create partitions

    Args:
        athena_client (boto3.client): Instantiated CLI AthenaClient
        options (namedtuple): The parsed args passed from the CLI
        config (CLIConfig): Loaded StreamAlert CLI
    """
    if not options.table_name:
        LOGGER_CLI.error('Missing command line argument --table_name')
        return

    if not options.bucket:
        LOGGER_CLI.error('Missing command line argument --bucket')
        return

    if options.type == 'data':
        # Get the current set of partitions
        partition_success, partitions = athena_client.run_athena_query(
            query='SHOW PARTITIONS {}'.format(options.table_name), database='streamalert')
        if not partition_success:
            LOGGER_CLI.error('An error occured when loading partitions for %s', options.table_name)
            return

        unique_partitions = athena_helpers.unique_values_from_query(partitions)

        # Drop the table
        LOGGER_CLI.info('Dropping table %s', options.table_name)
        drop_success, _ = athena_client.run_athena_query(
            query='DROP TABLE {}'.format(options.table_name), database='streamalert')
        if not drop_success:
            LOGGER_CLI.error('An error occured when dropping the %s table', options.table_name)
            return

        LOGGER_CLI.info('Dropped table %s', options.table_name)

        new_partitions_statement = athena_helpers.partition_statement(
            unique_partitions, options.bucket, options.table_name)

        # Re-create the table with previous partitions
        options.refresh_type = 'add_hive_partition'
        create_table(athena_client, options, config)

        LOGGER_CLI.info('Creating %d new partitions for %s',
                        len(unique_partitions), options.table_name)
        new_part_success, _ = athena_client.run_athena_query(
            query=new_partitions_statement, database='streamalert')
        if not new_part_success:
            LOGGER_CLI.error('Error re-creating new partitions for %s', options.table_name)
            return

        LOGGER_CLI.info('Successfully rebuilt partitions for %s', options.table_name)

    else:
        LOGGER_CLI.info('Refreshing alerts tables unsupported')


def drop_all_tables(athena_client):
    """Drop all 'streamalert' Athena tables

    Used when cleaning up an existing deployment

    Args:
        athena_client (boto3.client): Instantiated CLI AthenaClient
    """
    if not continue_prompt(message='Are you sure you want to drop all Athena tables?'):
        return

    success, all_tables = athena_client.run_athena_query(
        query='SHOW TABLES', database='streamalert')
    if not success:
        LOGGER_CLI.error('There was an issue getting all tables')
        return

    unique_tables = athena_helpers.unique_values_from_query(all_tables)

    for table in unique_tables:
        success, all_tables = athena_client.run_command(
            query='DROP TABLE {}'.format(table), database='streamalert')
        if not success:
            LOGGER_CLI.error('Unable to drop the %s table', table)
        else:
            LOGGER_CLI.info('Dropped %s', table)


def _add_to_athena_schema(log_schema, athena_schema, root_key=''):
    """Add sanitized schemas to the Athena table schema

    Args:
        log_schema (dict): The related StreamAlert log schema
        athena_schema (dict): The Athena table schema to add to
        root_key (str): The hierarchy where the schema should be added
    """
    # Setup the root_key dict
    if root_key and not athena_schema.get(root_key):
        athena_schema[root_key] = {}

    for key_name, key_type in log_schema.iteritems():
        # When using special characters in the beginning or end
        # of a column name, they have to be wrapped in backticks
        key_name = '`{}`'.format(key_name)

        special_key = None
        # Transform the {} or [] into hashable types
        if key_type == {}:
            special_key = dict
        elif key_type == []:
            special_key = list
        # Cast nested dict as a string for now
        # TODO(jacknagz): support recursive schemas
        elif isinstance(key_type, dict):
            special_key = 'string'

        # Account for envelope keys
        if root_key:
            if special_key is not None:
                athena_schema[root_key][key_name] = SCHEMA_TYPE_MAPPING[special_key]
            else:
                athena_schema[root_key][key_name] = SCHEMA_TYPE_MAPPING[key_type]
        else:
            if special_key is not None:
                athena_schema[key_name] = SCHEMA_TYPE_MAPPING[special_key]
            else:
                athena_schema[key_name] = SCHEMA_TYPE_MAPPING[key_type]


def create_table(athena_client, options, config):
    """Create a 'streamalert' Athena table

    Args:
        athena_client (boto3.client): Instantiated CLI AthenaClient
        options (namedtuple): The parsed args passed from the CLI
        config (CLIConfig): Loaded StreamAlert CLI
    """
    if not options.bucket:
        LOGGER_CLI.error('Missing command line argument --bucket')
        return

    if not options.refresh_type:
        LOGGER_CLI.error('Missing command line argument --refresh_type')
        return

    if options.type == 'data':
        if not options.table_name:
            LOGGER_CLI.error('Missing command line argument --table_name')
            return

        if options.table_name not in terraform_cli_helpers.enabled_firehose_logs(config):
            LOGGER_CLI.error('Table name %s missing from configuration or '
                             'is not enabled.', options.table_name)
            return

        if athena_client.check_table_exists(options.table_name):
            LOGGER_CLI.info('The \'%s\' table already exists.', options.table_name)
            return

        log_info = config['logs'][options.table_name.replace('_', ':', 1)]
        schema = dict(log_info['schema'])
        schema_statement = ''

        sanitized_schema = StreamAlert.sanitize_keys(schema)
        athena_schema = {}

        _add_to_athena_schema(sanitized_schema, athena_schema)

        # Support envelope keys
        configuration_options = log_info.get('configuration')
        if configuration_options:
            envelope_keys = configuration_options.get('envelope_keys')
            if envelope_keys:
                sanitized_envelope_key_schema = StreamAlert.sanitize_keys(envelope_keys)
                # Note: this key is wrapped in backticks to be Hive compliant
                _add_to_athena_schema(sanitized_envelope_key_schema, athena_schema,
                                      '`streamalert:envelope_keys`')

        for key_name, key_type in athena_schema.iteritems():
            # Account for nested structs
            if isinstance(key_type, dict):
                struct_schema = ''.join([
                    '{0}:{1},'.format(sub_key, sub_type)
                    for sub_key, sub_type in key_type.iteritems()
                ])
                nested_schema_statement = '{0} struct<{1}>, '.format(
                    key_name,
                    # Use the minus index to remove the last comma
                    struct_schema[:-1])
                schema_statement += nested_schema_statement
            else:
                schema_statement += '{0} {1},'.format(key_name, key_type)

        query = (
            'CREATE EXTERNAL TABLE {table_name} ({schema}) '
            'PARTITIONED BY (dt string) '
            'ROW FORMAT SERDE \'org.openx.data.jsonserde.JsonSerDe\' '
            'LOCATION \'s3://{bucket}/{table_name}/\''.format(
                table_name=options.table_name,
                # Use the minus index to remove the last comma
                schema=schema_statement[:-1],
                bucket=options.bucket))

    elif options.type == 'alerts':
        if athena_client.check_table_exists(options.type):
            LOGGER_CLI.info('The \'alerts\' table already exists.')
            return

        query = ('CREATE EXTERNAL TABLE alerts ('
                 'log_source string,'
                 'log_type string,'
                 'outputs array<string>,'
                 'record string,'
                 'rule_description string,'
                 'rule_name string,'
                 'source_entity string,'
                 'source_service string)'
                 'PARTITIONED BY (dt string)'
                 'ROW FORMAT SERDE \'org.openx.data.jsonserde.JsonSerDe\''
                 'LOCATION \'s3://{bucket}/alerts/\''.format(bucket=options.bucket))

    if query:
        create_table_success, _ = athena_client.run_athena_query(
            query=query, database='streamalert')

        if create_table_success:
            # Update the CLI config
            config['lambda']['athena_partition_refresh_config'] \
                  ['refresh_type'][options.refresh_type][options.bucket] = options.type
            config.write()

            table_name = options.type if options.type == 'alerts' else options.table_name
            LOGGER_CLI.info('The %s table was successfully created!', table_name)


def athena_handler(options, config):
    """Main Athena handler

    Args:
        options (namedtuple): The parsed args passed from the CLI
        config (CLIConfig): Loaded StreamAlert CLI
    """
    athena_client = StreamAlertAthenaClient(config, results_key_prefix='stream_alert_cli')

    if options.subcommand == 'init':
        config.generate_athena()

    elif options.subcommand == 'enable':
        config.set_athena_lambda_enable()

    elif options.subcommand == 'create-db':
        create_database(athena_client)

    elif options.subcommand == 'rebuild-partitions':
        rebuild_partitions(athena_client, options, config)

    elif options.subcommand == 'drop-all-tables':
        drop_all_tables(athena_client)

    elif options.subcommand == 'create-table':
        create_table(athena_client, options, config)
