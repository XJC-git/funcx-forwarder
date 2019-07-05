import psycopg2.extras
import pickle
import uuid
import json
import time
import statistics
import base64

from .utils import (_get_user, _create_task, _update_task, _log_request, 
                    _register_site, _register_function, _resolve_endpoint,
                    _resolve_function, _introspect_token, _get_container)
from flask import current_app as app, Blueprint, jsonify, request, abort
from config import _get_db_connection, _get_redis_client
from utils.majordomo_client import ZMQClient

import threading

# Flask
api = Blueprint("api", __name__)

zmq_client = ZMQClient("tcp://localhost:50001")

token_cache = {}
caching = True


@api.route('/execute', methods=['POST'])
def execute():
    """Puts a job in Redis and returns an id

    Returns
    -------
    json
        The task document
    """
    token = None
    if 'Authorization' in request.headers:
        token = request.headers.get('Authorization')
        token = token.split(" ")[1]
    else:
        abort(400, description="Error: You must be logged in to perform this function.")

    if caching and token in token_cache:
        user_name = token_cache[token]
    else:
        # Perform an Auth call to get the user name
        user_name = _introspect_token(request.headers)
        token_cache.update({token: user_name})

    if not user_name:
        abort(400, description="Error: You must be logged in to perform this function.")

    try:
        post_req = request.json
        endpoint = post_req['endpoint']
        function_uuid = post_req['func']
        input_data = post_req['data']

        task_id = str(uuid.uuid4())

        if 'action_id' in post_req:
            task_id = post_req['action_id']

        app.logger.info("Task assigned UUID: ".format(task_id))

        # Get the redis connection
        rc = _get_redis_client()

        # Add the job to redis
        task_payload = {'endpoint_id': endpoint,
                        'function_id': function_uuid,
                        'input_data': input_data,
                        'user_name': user_name,
                        'status': 'PENDING'}

        rc.set(task_id, json.dumps(task_payload))

        # Add the task to the redis queue
        rc.rpush("task_list", task_id)

    except Exception as e:
        app.logger.error(e)

    return jsonify({'task_id': task_id})


@api.route("/<task_uuid>/status", methods=['GET'])
def status(task_uuid):
    """Check the status of a task.

    Parameters
    ----------
    task_uuid : str
        The task uuid to look up

    Returns
    -------
    json
        The status of the task
    """

    user_id, user_name, short_name = _get_user(request.headers)

    conn, cur = _get_db_connection()

    try:
        task_status = None
        cur.execute("select tasks.*, results.result from tasks, results where tasks.uuid = %s and tasks.uuid = "
                    "results.task_id;", (task_uuid,))
        rows = cur.fetchall()
        app.logger.debug("Num rows w/ matching UUID: ".format(rows))
        for r in rows:
            app.logger.debug(r)
            task_status = r['status']
            try:
                task_result = r['result']
            except:
                pass
        
        res = {'status': task_status}
        if task_result:
            res.update({'details': {'result': pickle.loads(base64.b64decode(task_result.encode()))}})

        print("Status Response: {}".format(str(res)))
        return json.dumps(res)

    except Exception as e:
        app.logger.error(e)
        return json.dumps({'InternalError': e})


@api.route("/<task_uuid>/result", methods=['GET'])
def result(task_uuid):
    """Check the result of a task.

    Parameters
    ----------
    task_uuid : str
        The task uuid to look up

    Returns
    -------
    json
        The result of the task
    """

    # TODO merge this with status and return a details branch when a result exists.

    user_id, user_name, short_name = _get_user(request.headers)

    conn, cur = _get_db_connection()

    try:
        result = None
        cur.execute("SELECT result FROM results WHERE task_id = '%s'" % task_uuid)
        rows = cur.fetchall()
        app.logger.debug("Num rows w/ matching UUID: ".format(rows))
        for r in rows:
            result = r['result']
        res = {'result': pickle.loads(base64.b64decode(result.encode()))}
        app.logger.debugt("Result Response: {}".format(str(res)))
        return json.dumps(res)

    except Exception as e:
        app.logger.error(e)
        return json.dumps({'InternalError': e})


@api.route("/containers/<container_id>/<container_type>", methods=['GET'])
def get_container(container_id, container_type):
    """Get the details of a container.

    Parameters
    ----------
    container_id : str
        The id of the container
    container_type : str
        The type of containers to return: Docker, Singularity, Shifter, etc.

    Returns
    -------
    dict
        A dictionary of container details
    """
    user_id, user_name, short_name = _get_user(request.headers)
    if not user_name:
        abort(400, description="Error: You must be logged in to perform this function.")
    app.logger.debug(f"Getting container details: {container_id}")
    container = _get_container(user_id, container_id, container_type)
    print(container)
    return jsonify({'container': container})


@api.route("/register_endpoint", methods=['POST'])
def register_site():
    """Register the site. Add this site to the database and associate it with this user.

    Returns
    -------
    json
        A dict containing the endpoint details
    """
    user_id, user_name, short_name = _get_user(request.headers)
    if not user_name:
        abort(400, description="Error: You must be logged in to perform this function.")
    endpoint_name = None
    description = None
    endpoint_uuid = None
    try:
        endpoint_name = request.json["endpoint_name"]
        description = request.json["description"]
    except Exception as e:
        app.logger.error(e)

    if 'endpoint_uuid' in request.json:
        endpoint_uuid = request.json["endpoint_uuid"]

    app.logger.debug(endpoint_name)
    endpoint_uuid = _register_site(user_id, endpoint_name, description, endpoint_uuid)
    return jsonify({'endpoint_uuid': endpoint_uuid})


@api.route("/register_function", methods=['POST'])
def register_function():
    """Register the function.

    Returns
    -------
    json
        Dict containing the function details
    """
    user_id, user_name, short_name = _get_user(request.headers)
    if not user_name:
        abort(400, description="Error: You must be logged in to perform this function.")
    try:
        function_name = request.json["function_name"]
        entry_point = request.json["entry_point"]
        description = request.json["description"]
        function_code = request.json["function_code"]
    except Exception as e:
        app.logger.error(e)
    app.logger.debug(function_name)
    function_uuid = _register_function(user_id, function_name, description, function_code, entry_point)
    return jsonify({'function_uuid': function_uuid})

