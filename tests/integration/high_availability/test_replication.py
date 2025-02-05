#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.


import logging
import subprocess
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest

from tests.integration.helpers import (
    app_name,
    cluster_name,
    execute_queries_on_unit,
    generate_random_string,
    get_primary_unit,
    get_primary_unit_wrapper,
    get_server_config_credentials,
    scale_application,
)
from tests.integration.high_availability.high_availability_helpers import (
    clean_up_database_and_table,
    ensure_all_units_continuous_writes_incrementing,
    ensure_n_online_mysql_members,
    high_availability_test_setup,
    insert_data_into_mysql_and_validate_replication,
    pack_charm,
)
from tests.integration.integration_constants import SERIES_TO_VERSION

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
ANOTHER_APP_NAME = f"second{APP_NAME}"
TIMEOUT = 17 * 60


@pytest.mark.abort_on_fail
@pytest.mark.ha_tests
async def test_build_and_deploy(ops_test: OpsTest, series: str) -> None:
    """Build the charm and deploy 3 units to ensure a cluster is formed."""
    await high_availability_test_setup(ops_test, series)


@pytest.mark.abort_on_fail
@pytest.mark.ha_tests
async def test_consistent_data_replication_across_cluster(
    ops_test: OpsTest,
) -> None:
    """Confirm that data is replicated from the primary node to all the replicas."""
    mysql_application_name, _ = await high_availability_test_setup(ops_test)

    # assert that there are 3 units in the mysql cluster
    assert len(ops_test.model.applications[mysql_application_name].units) == 3

    database_name, table_name = "test-check-consistency", "data"
    await insert_data_into_mysql_and_validate_replication(ops_test, database_name, table_name)
    await clean_up_database_and_table(ops_test, database_name, table_name)

    await ensure_all_units_continuous_writes_incrementing(ops_test)


@pytest.mark.abort_on_fail
@pytest.mark.ha_tests
async def test_kill_primary_check_reelection(ops_test: OpsTest) -> None:
    """Confirm that a new primary is elected when the current primary is torn down."""
    mysql_application_name, _ = await high_availability_test_setup(ops_test)
    application = ops_test.model.applications[mysql_application_name]

    await ensure_all_units_continuous_writes_incrementing(ops_test)

    primary_unit = await get_primary_unit_wrapper(ops_test, mysql_application_name)
    primary_unit_name = primary_unit.name

    # Destroy the primary unit and block to ensure that the
    # juju status changed from active
    logger.info("Destroying leader unit")
    await ops_test.model.destroy_units(primary_unit.name)

    async with ops_test.fast_forward():
        await ops_test.model.block_until(lambda: len(application.units) == 2)
        await ops_test.model.wait_for_idle(
            apps=[mysql_application_name],
            status="active",
            raise_on_blocked=True,
            timeout=TIMEOUT,
        )

    # Wait for unit to be destroyed and confirm that the new primary unit is different
    new_primary_unit = await get_primary_unit_wrapper(ops_test, mysql_application_name)

    assert primary_unit_name != new_primary_unit.name, "Primary has not changed"

    # Add the unit back and wait until it is active
    async with ops_test.fast_forward():
        logger.info("Scaling back to 3 units")
        await scale_application(ops_test, mysql_application_name, 3)

        # wait (and retry) until the killed pod is back online in the mysql cluster
        assert await ensure_n_online_mysql_members(
            ops_test, 3
        ), "Old primary has not come back online after being killed"

    await ensure_all_units_continuous_writes_incrementing(ops_test)

    database_name, table_name = "test-kill-primary-check-reelection", "data"
    await insert_data_into_mysql_and_validate_replication(ops_test, database_name, table_name)
    await clean_up_database_and_table(ops_test, database_name, table_name)


@pytest.mark.abort_on_fail
@pytest.mark.ha_tests
async def test_scaling_without_data_loss(ops_test: OpsTest) -> None:
    """Test that data is preserved during scale up and scale down."""
    # Insert values into test table from the primary unit
    app = await app_name(ops_test)
    application = ops_test.model.applications[app]

    random_unit = application.units[0]
    server_config_credentials = await get_server_config_credentials(random_unit)

    primary_unit = await get_primary_unit_wrapper(
        ops_test,
        app,
    )
    primary_unit_address = await primary_unit.get_public_address()

    random_chars = generate_random_string(40)
    create_records_sql = [
        "CREATE DATABASE IF NOT EXISTS test",
        "CREATE TABLE IF NOT EXISTS test.instance_state_replication (id varchar(40), primary key(id))",
        f"INSERT INTO test.instance_state_replication VALUES ('{random_chars}')",
    ]

    await execute_queries_on_unit(
        primary_unit_address,
        server_config_credentials["username"],
        server_config_credentials["password"],
        create_records_sql,
        commit=True,
    )

    old_unit_names = [unit.name for unit in ops_test.model.applications[app].units]

    # Add a unit and wait until it is active
    async with ops_test.fast_forward():
        await scale_application(ops_test, app, 4)

    added_unit = [unit for unit in application.units if unit.name not in old_unit_names][0]

    # Ensure that all units have the above inserted data
    select_data_sql = [
        f"SELECT * FROM test.instance_state_replication WHERE id = '{random_chars}'",
    ]

    for unit in application.units:
        unit_address = await unit.get_public_address()
        output = await execute_queries_on_unit(
            unit_address,
            server_config_credentials["username"],
            server_config_credentials["password"],
            select_data_sql,
        )
        assert random_chars in output

    # Destroy the recently created unit and wait until the application is active
    await ops_test.model.destroy_units(added_unit.name)
    async with ops_test.fast_forward():
        await ops_test.model.block_until(lambda: len(ops_test.model.applications[app].units) == 3)
        await ops_test.model.wait_for_idle(
            apps=[app],
            status="active",
            raise_on_blocked=True,
            timeout=TIMEOUT,
        )

    # Ensure that the data still exists in all the units
    for unit in application.units:
        unit_address = await unit.get_public_address()
        output = await execute_queries_on_unit(
            unit_address,
            server_config_credentials["username"],
            server_config_credentials["password"],
            select_data_sql,
        )
        assert random_chars in output


@pytest.mark.ha_tests
async def test_cluster_isolation(ops_test: OpsTest, series: str) -> None:
    """Test for cluster data isolation.

    This test creates a new cluster, create a new table on both cluster, write a single record with
    the application name for each cluster, retrieve and compare these records, asserting they are
    not the same.
    """
    app = await app_name(ops_test)
    apps = [app, ANOTHER_APP_NAME]

    # Build and deploy secondary charm
    charm_url = pack_charm(series)
    if not charm_url:
        # Manually call charmcraft pack because ops_test.build_charm() does not support
        # multiple bases in the charmcraft file
        charmcraft_pack_commands = ["sg", "lxd", "-c", "charmcraft pack"]
        subprocess.check_output(charmcraft_pack_commands)
        charm_url = f"local:mysql_ubuntu-{SERIES_TO_VERSION[series]}-amd64.charm"

    await ops_test.model.deploy(
        charm_url,
        application_name=ANOTHER_APP_NAME,
        num_units=1,
        series=series,
    )
    async with ops_test.fast_forward():
        await ops_test.model.block_until(
            lambda: len(ops_test.model.applications[ANOTHER_APP_NAME].units) == 1
        )
        await ops_test.model.wait_for_idle(
            apps=[ANOTHER_APP_NAME],
            status="active",
            raise_on_blocked=True,
            timeout=TIMEOUT,
        )

    # retrieve connection data for each cluster
    connection_data = dict()
    for application in apps:
        random_unit = ops_test.model.applications[application].units[0]
        cluster = cluster_name(random_unit, ops_test.model.info.name)
        server_config_credentials = await get_server_config_credentials(random_unit)
        primary_unit = await get_primary_unit(
            ops_test,
            random_unit,
            application,
            cluster,
            server_config_credentials["username"],
            server_config_credentials["password"],
        )

        primary_unit_address = await primary_unit.get_public_address()

        connection_data[application] = {
            "host": primary_unit_address,
            "username": server_config_credentials["username"],
            "password": server_config_credentials["password"],
        }

    # write single distinct record to each cluster
    for application in apps:
        create_records_sql = [
            "CREATE DATABASE IF NOT EXISTS test",
            "DROP TABLE IF EXISTS test.cluster_isolation_table",
            "CREATE TABLE test.cluster_isolation_table (id varchar(40), primary key(id))",
            f"INSERT INTO test.cluster_isolation_table VALUES ('{application}')",
        ]

        await execute_queries_on_unit(
            connection_data[application]["host"],
            connection_data[application]["username"],
            connection_data[application]["password"],
            create_records_sql,
            commit=True,
        )

    result = list()
    # read single record from each cluster
    for application in apps:
        read_records_sql = ["SELECT id FROM test.cluster_isolation_table"]

        output = await execute_queries_on_unit(
            connection_data[application]["host"],
            connection_data[application]["username"],
            connection_data[application]["password"],
            read_records_sql,
            commit=False,
        )

        assert len(output) == 1, "Just one record must exist on the test table"
        result.append(output[0])

    assert result[0] != result[1], "Writes from one cluster are replicated to another cluster."
