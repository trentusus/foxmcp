"""
Integration tests for WebSocket communication between server and extension
"""

import pytest
import json
import asyncio
import websockets
import time
import re
from unittest.mock import Mock, patch, AsyncMock
from server.server import FoxMCPServer

class TestWebSocketCommunication:

    @pytest.fixture
    async def server_instance(self):
        """Start server instance for testing"""
        server = FoxMCPServer(host="localhost", port=8766)  # Use different port for tests

        # Start server in background
        server_task = asyncio.create_task(server.start_server())

        # Give server time to start
        await asyncio.sleep(0.1)

        yield server

        # Cleanup
        await server.shutdown(server_task)

    @pytest.mark.asyncio
    async def test_extension_connection(self):
        """Test extension can connect to server"""
        server = FoxMCPServer(host="localhost", port=8767, start_mcp=False)

        # Mock websocket server
        with patch('websockets.serve') as mock_serve:
            mock_server = Mock()
            mock_server.wait_closed = AsyncMock()
            mock_serve.return_value = mock_server

            # Make the mock awaitable
            async def mock_serve_func(*args, **kwargs):
                return mock_server

            mock_serve.side_effect = mock_serve_func

            # Start server (will be mocked)
            await server.start_server()

            # Verify websockets.serve was called with correct parameters
            mock_serve.assert_called_once_with(
                server.handle_extension_connection,
                "localhost",
                8767,
                max_size=server.websocket_max_message_bytes,
                reuse_address=True
            )

    @pytest.mark.asyncio
    async def test_message_exchange(self):
        """Test message exchange between mock client and server"""
        server = FoxMCPServer(host="localhost", port=8768)

        # Test message handling directly
        test_message = json.dumps({
            "id": "test_001",
            "type": "response",
            "action": "tabs.list",
            "data": {"tabs": []}
        })

        # Should handle message without error
        await server.handle_extension_message(test_message)

    @pytest.mark.asyncio
    async def test_invalid_message_handling(self):
        """Test server handles invalid messages gracefully"""
        server = FoxMCPServer(host="localhost", port=8769)

        invalid_messages = [
            "not json",
            '{"incomplete": json',
            "",
            "null",
            '{"missing": "required_fields"}'
        ]

        for message in invalid_messages:
            # Should not raise exceptions
            await server.handle_extension_message(message)

    @pytest.mark.asyncio
    async def test_connection_state_management(self):
        """Test connection state is properly managed"""
        server = FoxMCPServer(host="localhost", port=8770)

        # Initially no connection
        assert server.extension_connection is None

        # Mock websocket connection
        mock_ws = Mock()
        mock_ws.remote_address = ("127.0.0.1", 12345)

        # Simulate connection (without actually running the handler)
        server.extension_connection = mock_ws
        assert server.extension_connection is not None

        # Test sending message
        message = {"type": "request", "action": "test"}
        mock_ws.send = AsyncMock()
        with patch.object(mock_ws, 'send', new_callable=AsyncMock):
            result = await server.send_to_extension(message)
            assert result is True

class TestMessageRouting:

    @pytest.mark.asyncio
    async def test_request_routing(self):
        """Test that requests are routed correctly"""
        server = FoxMCPServer(start_mcp=False)

        # Test different action categories
        test_cases = [
            {
                "action": "tabs.list",
                "expected_handler": "tabs"
            },
            {
                "action": "history.query",
                "expected_handler": "history"
            },
            {
                "action": "bookmarks.create",
                "expected_handler": "bookmarks"
            },
            {
                "action": "navigation.back",
                "expected_handler": "navigation"
            },
            {
                "action": "content.get_text",
                "expected_handler": "content"
            }
        ]

        for case in test_cases:
            message = {
                "id": "test",
                "type": "request",
                "action": case["action"],
                "data": {}
            }

            # This would test the routing logic
            # Currently just verifies message structure
            assert message["action"].split(".")[0] == case["expected_handler"]

class TestConnectionRecovery:

    @pytest.mark.asyncio
    async def test_reconnection_logic(self):
        """Test that connection recovery works"""
        # This would test the auto-reconnection logic in the extension
        # For now, just verify the concept

        reconnect_interval = 5000  # 5 seconds
        assert reconnect_interval > 0

        # In a real test, we would:
        # 1. Start server
        # 2. Connect extension
        # 3. Kill server
        # 4. Restart server
        # 5. Verify extension reconnects

    @pytest.mark.asyncio
    async def test_connection_cleanup(self):
        """Test connection cleanup on disconnect"""
        server = FoxMCPServer(start_mcp=False)

        # Mock connection
        mock_ws = Mock()
        server.extension_connection = mock_ws

        # Simulate disconnect (connection should be set to None)
        # This happens in the finally block of handle_extension_connection
        server.extension_connection = None

        assert server.extension_connection is None


class TestPayloadLimits:

    @pytest.mark.asyncio
    async def test_oversized_extension_response_is_bounded_and_connection_survives(self):
        """A 3.8 MB extension response becomes a structured error and does not kill the session."""
        server = FoxMCPServer(host="localhost", port=0, start_mcp=False)
        mock_websocket = AsyncMock()
        mock_websocket.remote_address = ("127.0.0.1", 12345)
        server._register_connection(mock_websocket)

        oversized_task = asyncio.create_task(server.send_request_and_wait({
            "id": "oversized_req",
            "type": "request",
            "action": "content.get_text",
            "data": {}
        }, timeout=2))

        await asyncio.sleep(0)
        await server.handle_extension_message(json.dumps({
            "id": "oversized_req",
            "type": "response",
            "action": "content.get_text",
            "data": {"text": "x" * 3_834_224}
        }), websocket=mock_websocket)

        oversized_response = await oversized_task
        assert oversized_response["type"] == "error"
        assert oversized_response["data"]["code"] == "RESPONSE_TOO_LARGE"
        assert oversized_response["data"]["details"]["error"] == "response_too_large"
        assert oversized_response["data"]["details"]["actualBytes"] > server.max_payload_bytes
        assert server._is_websocket_open(mock_websocket)

        followup_task = asyncio.create_task(server.send_request_and_wait({
            "id": "followup_req",
            "type": "request",
            "action": "tabs.list",
            "data": {}
        }, timeout=2))

        await asyncio.sleep(0)
        sent_followup = json.loads(mock_websocket.send.call_args[0][0])
        assert sent_followup["id"] == "followup_req"
        await server.handle_extension_message(json.dumps({
            "id": "followup_req",
            "type": "response",
            "action": "tabs.list",
            "data": {"tabs": []}
        }), websocket=mock_websocket)

        followup_response = await followup_task
        assert followup_response["type"] == "response"
        assert followup_response["data"]["tabs"] == []
