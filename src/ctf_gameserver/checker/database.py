import logging

import psycopg2

from ctf_gameserver.lib.database import transaction_cursor
from ctf_gameserver.lib.exceptions import DBDataError


def connect_to_dbs(game_db_host, game_db_name, game_db_user, game_db_password, state_db_host, state_db_name,
                   state_db_user, state_db_password):
    """
    Establishes Psycopg2 connections to the game and state databases.

    Returns:
        A tuple of the new connections to game and state database; the connections are None if they could not
        be established.
    """

    try:
        game_db_conn = psycopg2.connect(host=game_db_host, database=game_db_name, user=game_db_user,
                                        password=game_db_password)
    except psycopg2.OperationalError as e:
        logging.error('Could not establish connection to game database: %s', e)
        return (None, None)
    logging.info('Established connection to game database')

    try:
        state_db_conn = psycopg2.connect(host=state_db_host, database=state_db_name, user=state_db_user,
                                         password=state_db_password)
    except psycopg2.OperationalError as e:
        logging.error('Could not establish connection to state database: %s', e)
        return (None, None)
    logging.info('Established connection to state database')

    # Keep our mental model easy by always using (timezone-aware) UTC for dates and times
    with transaction_cursor(game_db_conn) as cursor:
        cursor.execute('SET TIME ZONE "UTC"')
    with transaction_cursor(state_db_conn) as cursor:
        cursor.execute('SET TIME ZONE "UTC"')

    return (game_db_conn, state_db_conn)


def get_control_info(db_conn, prohibit_changes=False):
    """
    Returns a dictionary containing relevant information about the competion, as stored in the game database.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT start, valid_ticks, tick_duration FROM scoring_gamecontrol')
        result = cursor.fetchone()

    if result is None:
        raise DBDataError('Game control information has not been configured')

    return {
        'contest_start': result[0],
        'valid_ticks': result[1],
        'tick_duration': result[2]
    }


def get_service_attributes(db_conn, service_slug, prohibit_changes=False):
    """
    Returns ID and name of a service for a given slug.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT id, name FROM scoring_service WHERE slug = %s', (service_slug,))
        result = cursor.fetchone()

    if result is None:
        raise DBDataError('Service has not been configured')

    return {
        'id': result[0],
        'name': result[1]
    }


def get_current_tick(db_conn, prohibit_changes=False):
    """
    Reads the current tick from the game database.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT current_tick FROM scoring_gamecontrol')
        result = cursor.fetchone()

    if result is None:
        raise DBDataError('Game control information has not been configured')

    return result[0]


def get_task_count(db_conn, service_id, prohibit_changes=False):
    """
    Returns the total number of tasks for the given service in the current tick.
    With our current Controller implementation, this should always be equal to the number of teams.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT COUNT(*)'
                       '    FROM scoring_flag flag, scoring_gamecontrol control'
                       '    WHERE flag.tick = control.current_tick'
                       '        AND flag.service_id = %s', (service_id,))
        result = cursor.fetchone()

    return result[0]


def get_new_tasks(db_conn, service_id, task_count, prohibit_changes=False):
    """
    Retrieves the given number of random open check tasks and marks them as in progress.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT flag.id, flag.protecting_team_id, flag.tick, team.net_number'
                       '    FROM scoring_flag flag, scoring_gamecontrol control, registration_team team'
                       '    WHERE flag.placement_start is NULL'
                       '        AND flag.tick = control.current_tick'
                       '        AND flag.service_id = %s'
                       '        AND flag.protecting_team_id = team.user_id'
                       '    ORDER BY RANDOM()'
                       '    LIMIT %s'
                       '    FOR UPDATE OF flag', (service_id, task_count))
        tasks = cursor.fetchall()

        # Mark placement as in progress
        cursor.executemany('UPDATE scoring_flag'
                           '    SET placement_start = NOW()'
                           '    WHERE id = %s', [(task[0],) for task in tasks])

    return [{
        'team_id': task[1],
        'team_net_no': task[3],
        'tick': task[2]
    } for task in tasks]


def commit_result(db_conn, service_id, team_net_no, tick, result, prohibit_changes=False, fake_team_id=None):
    """
    Saves the result from a Checker run to game database.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT user_id FROM registration_team'
                       '    WHERE net_number = %s', (team_net_no,))
        data = cursor.fetchone()
        if data is None:
            if fake_team_id is None:
                logging.error('No team found with net number %d, cannot commit result', team_net_no)
                return
            data = (fake_team_id,)
        team_id = data[0]

        cursor.execute('INSERT INTO scoring_statuscheck'
                       '    (service_id, team_id, tick, status, timestamp)'
                       '    VALUES (%s, %s, %s, %s, NOW())', (service_id, team_id, tick, result))
        # (In case of `prohibit_changes`,) PostgreSQL checks the database grants even if nothing is matched
        # by `WHERE`
        cursor.execute('UPDATE scoring_flag'
                       '    SET placement_end = NOW()'
                       '    WHERE service_id = %s AND protecting_team_id = %s AND tick = %s', (service_id,
                                                                                               team_id,
                                                                                               tick))


def load_state(db_conn, service_id, team_net_no, identifier, prohibit_changes=False):
    """
    Loads Checker data from state database.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        cursor.execute('SELECT data FROM checkerstate'
                       '    WHERE service_id = %s'
                       '        AND team_net_no = %s'
                       '        AND identifier = %s', (service_id, team_net_no, identifier))
        data = cursor.fetchone()

    if data is None:
        return None
    return data[0]


def store_state(db_conn, service_id, team_net_no, identifier, data, prohibit_changes=False):
    """
    Stores Checker data in state database.
    """

    with transaction_cursor(db_conn, prohibit_changes) as cursor:
        # (In case of `prohibit_changes`,) PostgreSQL checks the database grants even if no CONFLICT occurs
        cursor.execute('INSERT INTO checkerstate (service_id, team_net_no, identifier, data)'
                       '    VALUES (%s, %s, %s, %s)'
                       '    ON CONFLICT (service_id, team_net_no, identifier)'
                       '        DO UPDATE SET data = EXCLUDED.data', (service_id, team_net_no, identifier,
                                                                      data))
