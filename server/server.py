#!/usr/bin/env python3
"""
FoxMCP Server - WebSocket server that bridges browser extension with MCP clients

Copyright (c) 2024 FoxMCP Project
Licensed under the MIT License - see LICENSE file for details
"""

import argparse
import asyncio
import json
import logging
import socket
import sys
import os
import re
import threading
from datetime import datetime
from typing import Dict, Any, Optional
import uuid

import websockets
import uvicorn
try:
    from .mcp_tools import FoxMCPTools
except ImportError:
    from mcp_tools import FoxMCPTools

# Try to import port coordinator for dynamic port allocation
try:
    tests_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tests')
    sys.path.insert(0, tests_dir)
    from port_coordinator import get_port_by_type
    HAS_PORT_COORDINATOR = True
except ImportError:
    HAS_PORT_COORDINATOR = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_MAX_PAYLOAD_BYTES = int(os.environ.get("FOXMCP_MAX_PAYLOAD_BYTES", str(1024 * 1024)))
DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES = int(
    os.environ.get("FOXMCP_WEBSOCKET_MAX_MESSAGE_BYTES", str(max(DEFAULT_MAX_PAYLOAD_BYTES * 8, DEFAULT_MAX_PAYLOAD_BYTES)))
)

def find_available_port(start_port=3000, max_attempts=100):
    """Find an available port starting from start_port"""
    if HAS_PORT_COORDINATOR:
        # Use the fixed MCP port if available
        try:
            return get_port_by_type('mcp')
        except Exception:
            pass  # Fall through to traditional approach
    else:
        # Fallback to traditional approach
        for i in range(max_attempts):
            port = start_port + i
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('localhost', port))
                    return port
            except OSError:
                continue

    raise RuntimeError(f"Could not find available port starting from {start_port}")

class FoxMCPServer:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        mcp_port: int = None,
        start_mcp: bool = True,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        websocket_max_message_bytes: int = DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES
    ):
        self.host = host
        self.port = port
        self.max_payload_bytes = max_payload_bytes
        self.websocket_max_message_bytes = max(websocket_max_message_bytes, max_payload_bytes)

        # Set MCP port - default to 3000 for production, dynamic allocation only for tests
        if mcp_port is None:
            # Check if we're in a test environment by checking for pytest or explicit test indicators
            in_test_env = ('pytest' in sys.modules or
                          'PYTEST_CURRENT_TEST' in os.environ or
                          any('pytest' in path or 'test_' in os.path.basename(path) for path in sys.path))
            if in_test_env and HAS_PORT_COORDINATOR:
                # Use dynamic port allocation for tests to avoid conflicts
                self.mcp_port = find_available_port(3000)
            else:
                # Use fixed port 3000 for production
                self.mcp_port = 3000
        else:
            # Check if requested port is available
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('localhost', mcp_port))
                    self.mcp_port = mcp_port
            except OSError:
                logger.warning(f"Requested MCP port {mcp_port} is in use, finding alternative...")
                self.mcp_port = find_available_port(mcp_port)

        logger.info(f"MCP server will use port {self.mcp_port}")
        self.start_mcp = start_mcp

        # Backward-compatible alias for the selected extension connection.
        self.extension_connection = None
        self.active_connection_id = None
        self.connected_clients = {}  # Map of connection IDs to connection metadata
        self._websocket_connection_ids = {}  # Map of websocket objects to connection IDs
        self.pending_requests = {}  # Map of request IDs to Future objects

        # Connection event management
        self._connection_waiters = []  # List of futures waiting for connection

        # Initialize MCP tools
        self.mcp_tools = FoxMCPTools(self)
        self.mcp_app = self.mcp_tools.get_mcp_app()
        self.mcp_server_task = None
        self.mcp_thread = None
        self.mcp_server_instance = None
        self._shutdown_event = None
        self.websocket_server = None

    async def handle_extension_connection(self, websocket):
        """Handle WebSocket connection from browser extension
        """
        connection_id = self._register_connection(websocket)
        logger.info(f"Extension connected from {websocket.remote_address} as {connection_id}")

        # Notify all waiters that a connection has been established
        self._notify_connection_waiters()

        try:
            async for message in websocket:
                await self.handle_extension_message(message, websocket=websocket)
        except ConnectionAbortedError:
            logger.info(f"Extension disconnected: {connection_id}")
        except Exception as e:
            logger.error(f"Error handling extension connection {connection_id}: {e}")
        finally:
            self._unregister_connection(websocket)

    def _is_websocket_open(self, websocket) -> bool:
        if websocket is None:
            return False

        if getattr(websocket, "closed", False) is True:
            return False

        close_code = getattr(websocket, "close_code", None)
        if close_code is None:
            return True

        # Unit tests and older callers may assign plain Mock objects to
        # extension_connection; their attributes are mock sentinels, not close codes.
        return not isinstance(close_code, int)

    def _register_connection(self, websocket) -> str:
        connection_id = f"conn_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        self.connected_clients[connection_id] = {
            "id": connection_id,
            "websocket": websocket,
            "remote_address": str(getattr(websocket, "remote_address", "unknown")),
            "connected_at": now,
            "last_seen": now,
            "metadata": {},
        }
        self._websocket_connection_ids[websocket] = connection_id
        self.active_connection_id = connection_id
        self.extension_connection = websocket
        return connection_id

    def _unregister_connection(self, websocket):
        connection_id = self._websocket_connection_ids.pop(websocket, None)
        if not connection_id:
            return

        self.connected_clients.pop(connection_id, None)

        if self.active_connection_id == connection_id:
            self.active_connection_id = None
            self.extension_connection = None
            for candidate_id, connection in reversed(list(self.connected_clients.items())):
                candidate_websocket = connection.get("websocket")
                if self._is_websocket_open(candidate_websocket):
                    self.active_connection_id = candidate_id
                    self.extension_connection = candidate_websocket
                    break
        elif self.extension_connection is websocket:
            active_connection = self.connected_clients.get(self.active_connection_id or "")
            self.extension_connection = active_connection.get("websocket") if active_connection else None

    def _update_connection_metadata(self, websocket, metadata: Dict[str, Any]):
        connection_id = self._websocket_connection_ids.get(websocket)
        if not connection_id or connection_id not in self.connected_clients:
            return

        connection = self.connected_clients[connection_id]
        connection["metadata"].update(metadata or {})
        connection["last_seen"] = datetime.now().isoformat()
        logger.info(f"Updated extension metadata for {connection_id}: {connection['metadata']}")

    def _message_size_bytes(self, message) -> int:
        if isinstance(message, bytes):
            return len(message)
        return len(str(message).encode("utf-8"))

    def _extract_json_string_field(self, message: str, field: str) -> Optional[str]:
        match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', message)
        if not match:
            return None
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            return match.group(1)

    def _oversized_error_response(
        self,
        request_id: Optional[str],
        action: Optional[str],
        actual_bytes: int,
        source: str
    ) -> Dict[str, Any]:
        retry_hint = (
            "Retry with a smaller request, for example content_get_text(max_length=...), "
            "a narrower selector/script result, or save large screenshots to a file."
        )
        return {
            "id": request_id,
            "type": "error",
            "action": action or "",
            "data": {
                "code": "RESPONSE_TOO_LARGE",
                "message": (
                    f"{source} payload was {actual_bytes} bytes, exceeding the "
                    f"{self.max_payload_bytes} byte FoxMCP limit."
                ),
                "details": {
                    "error": "response_too_large",
                    "actualBytes": actual_bytes,
                    "maxBytes": self.max_payload_bytes,
                    "retryHint": retry_hint,
                }
            },
            "timestamp": datetime.now().isoformat()
        }

    async def _handle_oversized_extension_message(self, message: str, actual_bytes: int):
        request_id = self._extract_json_string_field(message, "id")
        action = self._extract_json_string_field(message, "action")
        message_type = self._extract_json_string_field(message, "type")

        logger.warning(
            "Rejected oversized extension %s for action %s (ID: %s): %s bytes exceeds %s",
            message_type or "message",
            action or "unknown",
            request_id or "unknown",
            actual_bytes,
            self.max_payload_bytes,
        )

        if request_id and request_id in self.pending_requests:
            await self.handle_extension_response(
                self._oversized_error_response(request_id, action, actual_bytes, "Extension response")
            )

    def _resolve_connection_id(self, connection_id: Optional[str] = None, profile: Optional[str] = None) -> Optional[str]:
        identifier = connection_id or profile
        if not identifier:
            return self.active_connection_id

        if identifier in self.connected_clients:
            return identifier

        identifier_lower = str(identifier).lower()
        matches = []
        for candidate_id, connection in self.connected_clients.items():
            metadata = connection.get("metadata", {})
            aliases = [
                candidate_id,
                metadata.get("connectionId"),
                metadata.get("profile"),
                metadata.get("profileName"),
                metadata.get("connectionName"),
                metadata.get("displayName"),
                metadata.get("extensionOrigin"),
            ]
            if any(alias and str(alias).lower() == identifier_lower for alias in aliases):
                matches.append(candidate_id)

        if len(matches) == 1:
            return matches[0]
        return None

    def list_connections(self):
        connections = []
        for connection_id, connection in self.connected_clients.items():
            websocket = connection.get("websocket")
            metadata = connection.get("metadata", {})
            connections.append({
                "id": connection_id,
                "active": connection_id == self.active_connection_id,
                "open": self._is_websocket_open(websocket),
                "remote_address": connection.get("remote_address"),
                "connected_at": connection.get("connected_at"),
                "last_seen": connection.get("last_seen"),
                "metadata": metadata,
            })
        return connections

    def select_connection(self, identifier: str) -> bool:
        connection_id = self._resolve_connection_id(connection_id=identifier)
        if not connection_id:
            return False

        websocket = self.connected_clients[connection_id].get("websocket")
        if not self._is_websocket_open(websocket):
            return False

        self.active_connection_id = connection_id
        self.extension_connection = websocket
        return True

    async def handle_extension_message(self, message: str, websocket=None):
        """Process message from browser extension"""
        try:
            actual_bytes = self._message_size_bytes(message)
            if actual_bytes > self.max_payload_bytes:
                await self._handle_oversized_extension_message(message, actual_bytes)
                return

            data = json.loads(message)
            message_type = data.get('type', 'unknown')
            message_id = data.get('id')
            action = data.get('action', 'unknown')

            # Handle debug logs specially - print them prominently
            if message_type == 'debug_log':
                level = data.get('data', {}).get('level', 'log')
                log_message = data.get('data', {}).get('message', '')
                timestamp = data.get('data', {}).get('timestamp', '')

                logger.info("----- EXTENSION DEBUG LOG -----")
                if level == 'error':
                    logger.info(f"🔴 EXTENSION ERROR [{timestamp}]: {log_message}")
                else:
                    logger.info(f"🔵 EXTENSION LOG [{timestamp}]: {log_message}")
                return

            if message_type in ['hello', 'connection.hello']:
                self._update_connection_metadata(websocket, data.get('data', {}))
                return

            logger.info(f"Received from extension: {message_type} - {action} (ID: {message_id})")

            if message_type == 'request':
                # Handle ping-pong for connection testing
                if action == 'ping':
                    await self.handle_ping_request(data, websocket=websocket)
                    return

            elif message_type in ['response', 'error']:
                # Handle response/error from extension
                await self.handle_extension_response(data)
                return

            # For other message types, just log for now
            logger.warning(f"Unhandled message type: {message_type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received: {message}")
        except Exception as e:
            logger.error(f"Error processing extension message: {e}")

    async def handle_extension_response(self, response_data: Dict[str, Any]):
        """Handle response or error from browser extension"""
        request_id = response_data.get('id')
        if not request_id:
            logger.warning("Received response without ID")
            return

        if request_id in self.pending_requests:
            future = self.pending_requests.pop(request_id)
            if not future.cancelled():
                future.set_result(response_data)
                logger.info(f"Completed pending request: {request_id}")
        else:
            logger.warning(f"Received response for unknown request: {request_id}")

    async def handle_ping_request(self, request: Dict[str, Any], websocket=None):
        """Handle ping request from extension"""
        response = {
            "id": request["id"],
            "type": "request",
            "action": "ping",
            "data": {"test": True},
            "timestamp": datetime.now().isoformat()
        }

        connection_id = self._websocket_connection_ids.get(websocket)
        success = await self.send_to_extension(response, connection_id=connection_id)
        if success:
            logger.info(f"Sent ping request to extension: {request['id']}")
        else:
            logger.error(f"Failed to send ping request: {request['id']}")


    async def test_ping_extension(self) -> Dict[str, Any]:
        """Send ping to extension and wait for response"""
        if not self._get_connection_websocket():
            return {"success": False, "error": "No extension connection"}

        test_id = f"server_ping_{int(datetime.now().timestamp() * 1000)}"
        ping_request = {
            "id": test_id,
            "type": "request",
            "action": "ping",
            "data": {"server_test": True},
            "timestamp": datetime.now().isoformat()
        }

        # Send ping and return immediately (for now)
        success = await self.send_to_extension(ping_request)
        if success:
            return {"success": True, "message": "Ping sent to extension", "id": test_id}
        else:
            return {"success": False, "error": "Failed to send ping"}

    def _get_connection_websocket(self, connection_id: Optional[str] = None, profile: Optional[str] = None):
        resolved_id = self._resolve_connection_id(connection_id=connection_id, profile=profile)
        if resolved_id:
            connection = self.connected_clients.get(resolved_id)
            websocket = connection.get("websocket") if connection else None
            if self._is_websocket_open(websocket):
                return websocket

        if not connection_id and not profile and self._is_websocket_open(self.extension_connection):
            return self.extension_connection

        return None

    async def send_to_extension(self, message: Dict[str, Any], connection_id: Optional[str] = None, profile: Optional[str] = None) -> bool:
        """Send message to browser extension"""
        websocket = self._get_connection_websocket(connection_id=connection_id, profile=profile)
        if not websocket:
            logger.warning("No extension connection available")
            return False

        try:
            message['timestamp'] = datetime.now().isoformat()
            serialized = json.dumps(message)
            size_bytes = self._message_size_bytes(serialized)
            if size_bytes > self.max_payload_bytes:
                logger.error(
                    "Refusing to send oversized server payload for action %s (ID: %s): %s bytes exceeds %s",
                    message.get("action", "unknown"),
                    message.get("id", "unknown"),
                    size_bytes,
                    self.max_payload_bytes,
                )
                return False
            await websocket.send(serialized)
            return True
        except Exception as e:
            logger.error(f"Error sending to extension: {e}")
            return False

    async def send_request_and_wait(
        self,
        request: Dict[str, Any],
        timeout: float = 30.0,
        connection_id: Optional[str] = None,
        profile: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send request to extension and wait for response"""
        request_id = request.get('id')
        if not request_id:
            raise ValueError("Request must have an ID")

        data = request.get("data") if isinstance(request.get("data"), dict) else {}
        connection_id = connection_id or data.pop("connection_id", None) or data.pop("connectionId", None)
        profile = profile or data.pop("profile", None)

        if not self._get_connection_websocket(connection_id=connection_id, profile=profile):
            target = connection_id or profile
            if target:
                return {"error": f"No extension connection available for {target}"}
            return {"error": "No extension connection available"}

        # Create future for response
        response_future = asyncio.Future()
        self.pending_requests[request_id] = response_future

        try:
            # Send the request
            success = await self.send_to_extension(request, connection_id=connection_id, profile=profile)
            if not success:
                self.pending_requests.pop(request_id, None)
                return {"error": "Failed to send request to extension"}

            # Wait for response with timeout
            response = await asyncio.wait_for(response_future, timeout=timeout)
            return response

        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            return {"error": f"Request timed out after {timeout} seconds"}
        except Exception as e:
            self.pending_requests.pop(request_id, None)
            return {"error": f"Request failed: {str(e)}"}

    # Test Helper Methods
    async def get_popup_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Get current popup display state from extension"""
        request = {
            "id": f"test_popup_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.get_popup_state",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def get_options_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Get current options page display state from extension"""
        request = {
            "id": f"test_options_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.get_options_state",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def get_storage_values(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Get raw storage.sync values from extension"""
        request = {
            "id": f"test_storage_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.get_storage_values",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def validate_ui_sync(self, expected_values: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Validate UI-storage synchronization with expected values"""
        request = {
            "id": f"test_validate_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.validate_ui_sync",
            "data": {"expectedValues": expected_values},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def refresh_ui_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Trigger UI state refresh in extension"""
        request = {
            "id": f"test_refresh_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.refresh_ui_state",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def visit_url_for_test(self, url: str, wait_time: float = 6.0, timeout: float = 20.0) -> Dict[str, Any]:
        """Visit a URL to create browser history entry for testing"""
        request = {
            "id": f"test_visit_url_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.visit_url",
            "data": {
                "url": url,
                "waitTime": int(wait_time * 1000)  # Convert to milliseconds
            },
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def visit_multiple_urls_for_test(self, urls: list, wait_time: float = 6.0, delay_between: float = 2.0, timeout: float = 90.0) -> Dict[str, Any]:
        """Visit multiple URLs to create browser history entries for testing"""
        request = {
            "id": f"test_visit_multiple_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.visit_multiple_urls",
            "data": {
                "urls": urls,
                "waitTime": int(wait_time * 1000),  # Convert to milliseconds
                "delayBetween": int(delay_between * 1000)  # Convert to milliseconds
            },
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def clear_test_history(self, urls: list = None, clear_all: bool = False, timeout: float = 30.0) -> Dict[str, Any]:
        """Clear test history entries for cleanup"""
        request = {
            "id": f"test_clear_history_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.clear_test_history",
            "data": {
                "urls": urls or [],
                "clearAll": clear_all
            },
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def test_storage_sync_workflow(self, test_values: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Complete test workflow: set values, validate sync, return results"""
        results = {
            "workflow_success": False,
            "steps": {},
            "errors": []
        }

        try:
            # Step 1: Get initial state
            initial_storage = await self.get_storage_values(timeout)
            if "error" in initial_storage:
                results["errors"].append(f"Failed to get initial storage: {initial_storage['error']}")
                return results
            results["steps"]["initial_storage"] = initial_storage

            # Step 2: Get popup state
            popup_state = await self.get_popup_state(timeout)
            if "error" in popup_state:
                results["errors"].append(f"Failed to get popup state: {popup_state['error']}")
                return results
            results["steps"]["popup_state"] = popup_state

            # Step 3: Get options state
            options_state = await self.get_options_state(timeout)
            if "error" in options_state:
                results["errors"].append(f"Failed to get options state: {options_state['error']}")
                return results
            results["steps"]["options_state"] = options_state

            # Step 4: Validate synchronization
            validation_result = await self.validate_ui_sync(test_values, timeout)
            if "error" in validation_result:
                results["errors"].append(f"Failed to validate UI sync: {validation_result['error']}")
                return results
            results["steps"]["validation"] = validation_result

            # Check if validation passed
            if validation_result.get("popupSyncValid") and validation_result.get("optionsSyncValid") and validation_result.get("storageMatches"):
                results["workflow_success"] = True
            else:
                results["errors"].extend(validation_result.get("issues", []))

            return results

        except Exception as e:
            results["errors"].append(f"Workflow exception: {str(e)}")
            return results

    def _notify_connection_waiters(self):
        """Notify all futures waiting for a connection"""
        for future in self._connection_waiters:
            if not future.cancelled():
                future.set_result(True)
        self._connection_waiters.clear()

    async def wait_for_extension_connection(self, timeout: float = 30.0) -> bool:
        """
        Wait for an extension connection to be established.

        Args:
            timeout: Maximum time to wait for connection in seconds

        Returns:
            bool: True if connection was established, False if timeout occurred

        Example:
            # Wait for Firefox extension to connect
            connected = await server.wait_for_extension_connection(timeout=10.0)
            if connected:
                print("Extension connected!")
            else:
                print("Connection timeout")
        """
        # If already connected, return immediately
        if self._get_connection_websocket():
            return True

        # Create a future to wait for connection
        connection_future = asyncio.Future()
        self._connection_waiters.append(connection_future)

        try:
            # Wait for connection with timeout
            await asyncio.wait_for(connection_future, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            # Remove the future from waiters if it timed out
            if connection_future in self._connection_waiters:
                self._connection_waiters.remove(connection_future)
            return False
        except Exception:
            # Remove the future from waiters on any other error
            if connection_future in self._connection_waiters:
                self._connection_waiters.remove(connection_future)
            return False

    async def start_mcp_server(self):
        """Start the MCP server in a separate thread"""
        import threading

        # Create shutdown event
        self._shutdown_event = threading.Event()

        def run_mcp_server():
            try:
                logger.info(f"Starting MCP server on {self.host}:{self.mcp_port}")

                # Create server config
                config = uvicorn.Config(
                    self.mcp_app.http_app(),
                    host=self.host,
                    port=self.mcp_port,
                    log_level="error"  # Reduce log noise during tests
                )

                # Create server instance
                self.mcp_server_instance = uvicorn.Server(config)

                # Run server
                self.mcp_server_instance.run()

            except Exception as e:
                logger.warning(f"MCP server failed to start on {self.host}:{self.mcp_port}: {e}")
                # Don't crash the whole server if MCP fails - this is important for tests

        # Run MCP server in separate thread
        self.mcp_thread = threading.Thread(target=run_mcp_server, daemon=True)
        self.mcp_thread.start()
        logger.info(f"MCP server thread started for {self.host}:{self.mcp_port}")

        # Give MCP server time to start (reduced time for faster tests)
        await asyncio.sleep(0.5)

    def _stop_mcp_server(self):
        """Stop the MCP server gracefully"""
        if self.mcp_server_instance:
            try:
                logger.info("Stopping MCP server...")

                # Signal server to shutdown
                self.mcp_server_instance.should_exit = True

                # Wait for thread to finish with timeout
                if self.mcp_thread and self.mcp_thread.is_alive():
                    self.mcp_thread.join(timeout=5.0)

                    if self.mcp_thread.is_alive():
                        logger.warning("MCP server thread did not stop gracefully within timeout")
                    else:
                        logger.info("MCP server stopped gracefully")

                # Clean up references
                self.mcp_server_instance = None
                self.mcp_thread = None

            except Exception as e:
                logger.warning(f"Error stopping MCP server: {e}")

    async def _stop_websocket_server(self):
        """Stop the WebSocket server gracefully"""
        if self.websocket_server:
            try:
                logger.info("Stopping WebSocket server...")

                # Close all existing connections first
                for connection_id, connection in list(self.connected_clients.items()):
                    websocket = connection.get("websocket")
                    try:
                        close_result = websocket.close()
                        if asyncio.iscoroutine(close_result):
                            await close_result
                        logger.info(f"Extension connection closed: {connection_id}")
                    except Exception as e:
                        logger.warning(f"Error closing extension connection {connection_id}: {e}")

                self.connected_clients.clear()
                self._websocket_connection_ids.clear()
                self.active_connection_id = None
                self.extension_connection = None

                # Close the WebSocket server
                self.websocket_server.close()

                # Clean up reference
                self.websocket_server = None

                logger.info("WebSocket server stopped gracefully")

            except Exception as e:
                logger.warning(f"Error stopping WebSocket server: {e}")

    async def _stop(self):
        """Stop all servers (WebSocket and MCP)"""
        logger.info("Stopping FoxMCP server...")

        # Stop MCP server
        self._stop_mcp_server()

        # Stop WebSocket server
        await self._stop_websocket_server()

        logger.info("FoxMCP server stopped")

    async def shutdown(self, server_task):
        """
        Gracefully shutdown the server and its task.

        Args:
            server_task: asyncio.Task running the server
        """
        try:
            # Stop server resources first
            await self._stop()

            # Cancel the task
            server_task.cancel()

            # Wait for task to finish, handling CancelledError
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        except Exception as e:
            logger.warning(f"Error during server shutdown: {e}")

    async def start_server(self):
        """Start both WebSocket and MCP servers"""
        logger.info(f"Starting FoxMCP server on {self.host}:{self.port}")

        # Start MCP server first (if enabled)
        if self.start_mcp:
            await self.start_mcp_server()
            logger.info(f"MCP tools available at http://{self.host}:{self.mcp_port}/")
        else:
            logger.info("MCP server disabled for this instance")

        # Use modern websockets API with SO_REUSEADDR
        import socket
        self.websocket_server = await websockets.serve(
            self.handle_extension_connection,
            self.host,
            self.port,
            max_size=self.websocket_max_message_bytes,
            reuse_address=True  # Enable SO_REUSEADDR for immediate port reuse
        )

        logger.info("FoxMCP WebSocket server is running...")
        await self.websocket_server.wait_closed()

async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='FoxMCP Server - WebSocket server for browser extension')
    parser.add_argument('--host', default='localhost',
                        help='Host to bind to (default: localhost)')
    parser.add_argument('--port', type=int, default=8765,
                        help='WebSocket port (default: 8765)')
    parser.add_argument('--mcp-port', type=int, default=None,
                        help='MCP server port (default: 3000, dynamic allocation in tests)')
    parser.add_argument('--no-mcp', action='store_true',
                        help='Disable MCP server')

    args = parser.parse_args()

    # Ensure localhost-only binding for security
    if args.host != 'localhost' and args.host != '127.0.0.1':
        logger.warning(f"Host '{args.host}' changed to 'localhost' for security")
        args.host = 'localhost'

    server = FoxMCPServer(
        host=args.host,
        port=args.port,
        mcp_port=args.mcp_port,
        start_mcp=not args.no_mcp
    )
    await server.start_server()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
