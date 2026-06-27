"""
Tests for multiple simultaneous Firefox extension WebSocket connections.
"""

import asyncio
import json

import pytest
import websockets
from unittest.mock import Mock

from server.server import FoxMCPServer


async def _start_server():
    server = FoxMCPServer(host="localhost", port=0, start_mcp=False)
    server_task = asyncio.create_task(server.start_server())

    for _ in range(50):
        if server.websocket_server and server.websocket_server.sockets:
            break
        await asyncio.sleep(0.02)

    assert server.websocket_server is not None
    assert server.websocket_server.sockets

    ipv4_socket = next(
        socket for socket in server.websocket_server.sockets
        if len(socket.getsockname()) == 2
    )
    port = ipv4_socket.getsockname()[1]
    return server, server_task, port


async def _send_response(websocket, request_message, data=None):
    await websocket.send(json.dumps({
        "id": request_message["id"],
        "type": "response",
        "action": request_message["action"],
        "data": data or {"ok": True},
    }))


@pytest.mark.asyncio
async def test_two_extension_connections_stay_open_and_store_hello_metadata():
    server, server_task, port = await _start_server()
    first = None
    second = None

    try:
        first = await websockets.connect(f"ws://127.0.0.1:{port}")
        second = await websockets.connect(f"ws://127.0.0.1:{port}")
        await asyncio.sleep(0.1)

        assert len(server.connected_clients) == 2
        first_id, second_id = list(server.connected_clients)
        assert server.active_connection_id == second_id
        assert server.extension_connection is server.connected_clients[second_id]["websocket"]

        await first.send(json.dumps({
            "id": "hello-foxmcp",
            "type": "hello",
            "action": "connection.hello",
            "data": {"profileName": "foxmcp", "connectionName": "foxmcp"},
        }))
        await second.send(json.dumps({
            "id": "hello-kodens",
            "type": "hello",
            "action": "connection.hello",
            "data": {"profileName": "kodens", "connectionName": "kodens"},
        }))
        await asyncio.sleep(0.1)

        assert first.close_code is None
        assert second.close_code is None
        assert server.connected_clients[first_id]["metadata"]["profileName"] == "foxmcp"
        assert server.connected_clients[second_id]["metadata"]["profileName"] == "kodens"

    finally:
        if first:
            await first.close()
        if second:
            await second.close()
        await server.shutdown(server_task)


@pytest.mark.asyncio
async def test_connection_mcp_tools_list_and_select_registered_sessions():
    server = FoxMCPServer(host="localhost", port=0, start_mcp=False)
    first = Mock()
    first.remote_address = ("127.0.0.1", 10001)
    first.close_code = None
    second = Mock()
    second.remote_address = ("127.0.0.1", 10002)
    second.close_code = None

    first_id = server._register_connection(first)
    second_id = server._register_connection(second)
    server._update_connection_metadata(first, {"profileName": "foxmcp"})
    server._update_connection_metadata(second, {"profileName": "kodens"})

    list_tool = await server.mcp_tools.mcp.get_tool("connections_list")
    select_tool = await server.mcp_tools.mcp.get_tool("connections_select")

    listed = await list_tool.fn()
    assert first_id in listed
    assert second_id in listed
    assert "foxmcp" in listed
    assert "kodens" in listed
    assert f"{second_id}: kodens" in listed
    assert "(active)" in listed

    selected = await select_tool.fn("foxmcp")
    assert first_id in selected
    assert server.active_connection_id == first_id
    assert server.extension_connection is first


@pytest.mark.asyncio
async def test_mcp_tab_navigation_content_tools_forward_explicit_route_fields():
    server = FoxMCPServer(host="localhost", port=0, start_mcp=False)
    captured = []

    async def capture_request(request, timeout=30.0, connection_id=None, profile=None):
        captured.append(request)
        action = request["action"]
        if action == "tabs.list":
            return {"type": "response", "data": {"tabs": []}}
        if action == "tabs.create":
            return {"type": "response", "data": {"tab": {"id": 10, "url": request["data"]["url"]}}}
        if action == "navigation.go_to_url":
            return {"type": "response", "data": {"success": True}}
        if action == "content.get_text":
            return {"type": "response", "data": {"text": "Example Domain", "url": "https://example.org", "title": "Example"}}
        return {"type": "response", "data": {}}

    server.send_request_and_wait = capture_request

    tabs_list = await server.mcp_tools.mcp.get_tool("tabs_list")
    tabs_create = await server.mcp_tools.mcp.get_tool("tabs_create")
    navigation_go_to_url = await server.mcp_tools.mcp.get_tool("navigation_go_to_url")
    content_get_text = await server.mcp_tools.mcp.get_tool("content_get_text")

    await tabs_list.fn(profile="kodens")
    await tabs_create.fn(url="https://example.org/explicit", connection_id="conn_test")
    await navigation_go_to_url.fn(tab_id=10, url="https://example.org/nav", profile="foxmcp")
    await content_get_text.fn(tab_id=10, max_length=100, connection_id="conn_text")

    assert captured[0]["data"]["profile"] == "kodens"
    assert captured[1]["data"]["connection_id"] == "conn_test"
    assert captured[2]["data"]["profile"] == "foxmcp"
    assert captured[3]["data"]["connection_id"] == "conn_text"
    assert captured[3]["data"]["maxLength"] == 100


@pytest.mark.asyncio
async def test_selected_default_and_explicit_connection_routing():
    server, server_task, port = await _start_server()
    first = None
    second = None

    try:
        first = await websockets.connect(f"ws://127.0.0.1:{port}")
        second = await websockets.connect(f"ws://127.0.0.1:{port}")
        await asyncio.sleep(0.1)
        first_id, second_id = list(server.connected_clients)

        await first.send(json.dumps({
            "id": "hello-foxmcp",
            "type": "hello",
            "action": "connection.hello",
            "data": {"profileName": "foxmcp"},
        }))
        await second.send(json.dumps({
            "id": "hello-kodens",
            "type": "hello",
            "action": "connection.hello",
            "data": {"profileName": "kodens"},
        }))
        await asyncio.sleep(0.1)

        default_request = {
            "id": "default-route",
            "type": "request",
            "action": "tabs.list",
            "data": {},
        }
        default_task = asyncio.create_task(
            server.send_request_and_wait(default_request, timeout=2)
        )
        default_message = json.loads(await asyncio.wait_for(second.recv(), timeout=2))
        assert default_message["id"] == "default-route"
        await _send_response(second, default_message, {"profile": "kodens"})
        assert (await default_task)["data"]["profile"] == "kodens"

        assert server.select_connection("foxmcp") is True
        assert server.active_connection_id == first_id
        selected_task = asyncio.create_task(
            server.send_request_and_wait({
                "id": "selected-route",
                "type": "request",
                "action": "tabs.list",
                "data": {},
            }, timeout=2)
        )
        selected_message = json.loads(await asyncio.wait_for(first.recv(), timeout=2))
        assert selected_message["id"] == "selected-route"
        await _send_response(first, selected_message, {"profile": "foxmcp"})
        assert (await selected_task)["data"]["profile"] == "foxmcp"

        explicit_id_task = asyncio.create_task(
            server.send_request_and_wait({
                "id": "explicit-id-route",
                "type": "request",
                "action": "tabs.list",
                "data": {"connection_id": second_id},
            }, timeout=2)
        )
        explicit_id_message = json.loads(await asyncio.wait_for(second.recv(), timeout=2))
        assert explicit_id_message["id"] == "explicit-id-route"
        assert "connection_id" not in explicit_id_message["data"]
        await _send_response(second, explicit_id_message, {"profile": "kodens"})
        assert (await explicit_id_task)["data"]["profile"] == "kodens"

        explicit_profile_task = asyncio.create_task(
            server.send_request_and_wait({
                "id": "explicit-profile-route",
                "type": "request",
                "action": "tabs.list",
                "data": {"profile": "foxmcp"},
            }, timeout=2)
        )
        explicit_profile_message = json.loads(await asyncio.wait_for(first.recv(), timeout=2))
        assert explicit_profile_message["id"] == "explicit-profile-route"
        assert "profile" not in explicit_profile_message["data"]
        await _send_response(first, explicit_profile_message, {"profile": "foxmcp"})
        assert (await explicit_profile_task)["data"]["profile"] == "foxmcp"

    finally:
        if first:
            await first.close()
        if second:
            await second.close()
        await server.shutdown(server_task)
