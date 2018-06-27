import asyncio
import logging
import os
import time
from os import path, sys

import aiohttp
import boto3

if True:
    sys.path.append(path.dirname(path.abspath(__file__)))
    from lib.db import get_table


logger = logging.getLogger(__name__)

DIFF_TOLERANCE = int(os.environ.get('DIFF_TOLERANCE', 10))


def get_block_numbers(event, context):
    table = get_table()
    items = table.scan()['Items']
    loop = asyncio.get_event_loop()
    backends = loop.run_until_complete(fetch_block_numbers(items))

    needs_global_update = False
    leader_block_number = get_leader_block_number(backends)

    for backend in backends:
        block_number = backend['block_number']
        was_healthy = backend['was_healthy']

        updates = {
            ':vblockNumber': block_number or backend['previous_block_number'],
        }
        if not block_number:
            is_healthy = False
        elif leader_block_number:
            is_healthy = (leader_block_number - block_number) < DIFF_TOLERANCE
        else:
            # all nodes are not healthy
            is_healthy = False

        updates[':vis_healthy'] = is_healthy

        if was_healthy is not is_healthy:
            needs_global_update = True

        table.update_item(
            Key={'url': backend['url']},
            UpdateExpression='SET block_number = :vblockNumber, is_healthy = :vis_healthy',
            ExpressionAttributeValues=updates)

    if needs_global_update:
        trigger_service_update()


def trigger_service_update():
    logger.info('Triggering update of service')
    boto3.client('lambda').invoke(
        FunctionName=os.environ['UPLOAD_SERVICE_CONFIG_ARN'],
        InvocationType='Event',
    )


def get_leader(backends):
    try:
        return [x for x in backends if x['is_leader']][0]
    except IndexError:
        return None


def get_leader_block_number(backends):
    leader = get_leader(backends)
    leader_block_number = leader and leader['block_number'] or None
    if not leader_block_number:
        leader_block_number = max(
            [backend['block_number'] or 0 for backend in backends])
    return leader_block_number


async def fetch_block_numbers(backends):
    async with aiohttp.ClientSession() as session:
        coros = [fetch_block_number(session, backend) for backend in backends]
        return await asyncio.gather(*coros)


timeout = aiohttp.ClientTimeout(total=2)


async def fetch_block_number(session, backend):
    data = {
        'jsonrpc': '2.0',
        'method': 'eth_blockNumber',
        'params': [],
        'id': 1
    }
    when_started = time.time()
    block_number = None
    url = backend['url']
    try:
        async with session.post(url, json=data, timeout=timeout) as response:
            if response.status == 200:
                response_body = await response.json()
                block_number = int(response_body['result'], 16)
            else:
                response_text = await response.text()
                logger.info(f'Got status code f{response.status} body: f{response_text}')

    except Exception as e:
        logger.exception('Failed to get blockNumber', extra={'url': url})
    return {
        'url': url,
        'previous_block_number': backend.get('block_number', 0),
        'block_number': block_number,
        'is_leader': backend['is_leader'],
        'elapsed': time.time() - when_started,
        'was_healthy': backend['is_healthy']
    }