# Server Configuration

Complete guide to configuring and running the FoxMCP server.

## Starting the Server

```bash
# Quick start (both WebSocket and MCP servers)
make run-server

# Custom configuration
python server/server.py --port 9000 --mcp-port 4000
python server/server.py --no-mcp  # WebSocket only, disable MCP server
```

## Command Line Options

```bash
python server/server.py [options]

Options:
  --host HOST          Host to bind to (default: localhost, security-enforced)
  --port PORT          WebSocket port (default: 8765)
  --mcp-port MCP_PORT  MCP server port (default: 3000)
  --no-mcp             Disable MCP server
  -h, --help           Show help message
```

## Security Features

- **Localhost-only binding**: Both WebSocket and MCP servers bind to `localhost` only for security
- **Host enforcement**: Any attempt to bind to external interfaces (e.g., `0.0.0.0`) is automatically changed to `localhost` with a warning
- **Default secure configuration**: No configuration required for secure localhost-only operation

## Server Ports

- **WebSocket Port**: Default `8765` - Used for Firefox extension communication
- **MCP Port**: Default `3000` - Used for MCP client connections

## Configuring Extension

The Firefox extension includes comprehensive configuration options with **storage.sync** persistence:

### 1. Access Options

- **Options Page**: Right-click extension → "Manage Extension" → "Preferences"
- **Popup Interface**: Click extension icon for quick configuration
- Or go to `about:addons` → FoxMCP → "Preferences"

### 2. Configure Connection

- **Hostname**: Server hostname (default: `localhost`)
- **WebSocket Port**: Server WebSocket port (default: `8765`)
- **Advanced Options**: Retry intervals, max retries, ping timeouts
- **Test Configuration**: Built-in test override system for development

### 3. Features

- **Real-time storage sync**: Configuration changes persist across browser restarts
- **Connection Status**: Real-time connection status monitoring
- **Status Indicators**: Live connection status with retry attempt information
- **Automatic Reconnection**: Extension automatically reconnects when settings change
- **Configuration Preservation**: Test settings maintained during normal use

## Programmatic Server Configuration

```python
# Default configuration (localhost-only, secure)
server = FoxMCPServer()  # WebSocket: localhost:8765, MCP: localhost:3000

# Custom ports (still localhost-only)
server = FoxMCPServer(host="localhost", port=9000, mcp_port=4000)

# WebSocket only (disable MCP)
server = FoxMCPServer(port=8765, start_mcp=False)
```

## MCP Client Connection

1. **Start the server** (both WebSocket and MCP servers)
2. **Load Firefox extension** (connects automatically to WebSocket)
3. **Connect MCP client** to `http://localhost:3000`

### Supported MCP Clients

**Claude Code**:
```bash
claude mcp add --transport http foxmcp http://localhost:3000/mcp/
```

**Other MCP Clients**:
Connect directly to `http://localhost:3000/mcp/`

**Complete Workflow**:
```
MCP Client → FastMCP Server → WebSocket → Firefox Extension → Browser API
```

## Environment Variables

### Required for Predefined Scripts

```bash
# Set path to your custom scripts directory
export FOXMCP_EXT_SCRIPTS="/path/to/your/scripts"
```

### Optional Configuration

```bash
# Override default ports
export FOXMCP_WEBSOCKET_PORT=8765
export FOXMCP_MCP_PORT=3000

# Debug mode
export FOXMCP_DEBUG=1
```

## Multiple Server Instances

FoxMCP can keep multiple extension connections open on one server. This is the preferred setup when you want one MCP namespace to operate against several Firefox profiles.

1. Start the normal server on WebSocket `8765` and MCP `3000`
2. Open both Firefox profiles with the FoxMCP extension installed
3. Use `connections_list` to see connected sessions
4. Use `connections_select` with a connection ID, or an exact configured profile/connection label, before running tab/content/navigation tools

Optional profile labels can be set in each Firefox profile's extension local or sync storage:

```javascript
await browser.storage.local.set({ profileName: "foxmcp" });
await browser.storage.local.set({ profileName: "kodens" });
```

Firefox WebExtensions do not expose the actual profile path or profile name to extensions, so unlabeled sessions must be selected by connection ID.

For local verification with an unsigned development build, `web-ext run` can load the extension temporarily into profile copies:

```bash
npx --yes web-ext run \
  --source-dir extension \
  --firefox /Applications/Firefox.app/Contents/MacOS/firefox \
  --firefox-profile "$HOME/Library/Application Support/Firefox/Profiles/foxmcp" \
  --no-reload --no-input --start-url about:blank --args=-no-remote
```

On standard Firefox builds, a copied XPI in a normal app profile may install but not run unless it is signed or loaded through the Firefox development workflow.

The local `foxmcp` profile can use a private unlisted Mozilla-signed build for persistent normal startup. This machine signs a local copy with add-on ID `foxmcp-local-trentusus@codemud.org`; the upstream `foxmcp@codemud.org` ID belongs to the public FoxMCP add-on/project and should not be reused for local AMO signing. After signing, install the signed XPI into:

```text
$HOME/Library/Application Support/Firefox/Profiles/foxmcp/extensions/foxmcp-local-trentusus@codemud.org.xpi
```

When rebuilding the signed local add-on, bump the manifest version or submit an update to the existing unlisted AMO add-on, then re-sign with `web-ext sign --channel=unlisted`. AMO API credentials should stay in 1Password or environment variables and must not be committed.

You can still run multiple FoxMCP servers on different ports when you need hard isolation or separate MCP namespaces:

```bash
# Server 1 - Default ports
python server/server.py

# Server 2 - Custom ports
python server/server.py --port 8766 --mcp-port 3001

# Server 3 - WebSocket only
python server/server.py --port 8767 --no-mcp
```

Separate server instances are simpler to reason about operationally, but each Firefox profile must point its extension at a different WebSocket port and each MCP client must connect to a different MCP URL. The multi-connection server keeps one MCP URL and avoids profile stealing, while routing is handled by connection selection.

## Docker Configuration

```dockerfile
FROM python:3.11

WORKDIR /app
COPY . .

RUN pip install -r requirements.txt

# Expose ports
EXPOSE 8765 3000

# Run server
CMD ["python", "server/server.py"]
```

```bash
# Build and run
docker build -t foxmcp .
docker run -p 8765:8765 -p 3000:3000 foxmcp
```

## Configuration Files

FoxMCP supports configuration files for persistent settings:

### `config.json` (Optional)

```json
{
  "server": {
    "host": "localhost",
    "websocket_port": 8765,
    "mcp_port": 3000,
    "enable_mcp": true
  },
  "security": {
    "localhost_only": true,
    "allow_external": false
  },
  "scripts": {
    "directory": "/path/to/scripts",
    "timeout": 30
  },
  "logging": {
    "level": "INFO",
    "file": "foxmcp.log"
  }
}
```

```bash
# Use configuration file
python server/server.py --config config.json
```

## Logging Configuration

### Basic Logging

```python
import logging

# Set log level
logging.basicConfig(level=logging.INFO)

# Start server with logging
server = FoxMCPServer()
```

### Advanced Logging

```python
import logging

# Configure detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('foxmcp.log'),
        logging.StreamHandler()
    ]
)

# Server will use configured logging
server = FoxMCPServer()
```

## Performance Tuning

### WebSocket Configuration

```python
# Adjust WebSocket settings for performance
server = FoxMCPServer(
    max_payload_bytes=1048576,  # Max FoxMCP application payload size
    websocket_max_message_bytes=8388608,  # Transport receive ceiling for classifying oversize responses
    ping_interval=20,  # Ping interval (seconds)
    ping_timeout=10,   # Ping timeout (seconds)
    close_timeout=10   # Close timeout (seconds)
)
```

Payload limits can also be set with `FOXMCP_MAX_PAYLOAD_BYTES` and `FOXMCP_WEBSOCKET_MAX_MESSAGE_BYTES`. Keep the WebSocket transport ceiling at or above the application payload limit so the server can return a structured `RESPONSE_TOO_LARGE` error instead of closing the connection with WebSocket code 1009. Tool authors should expose bounded options such as `max_length` or `max_result_bytes` for any page, DOM, screenshot, accessibility, or script result that can grow with webpage content.

### MCP Server Optimization

```python
# Configure FastMCP server
server = FoxMCPServer(
    mcp_workers=4,     # Number of worker threads
    mcp_timeout=30,    # Request timeout
    mcp_max_requests=100  # Max concurrent requests
)
```

## Troubleshooting Configuration

### Common Issues

1. **Port already in use**:
   ```bash
   # Check what's using the port
   lsof -i :8765

   # Use different port
   python server/server.py --port 8766
   ```

2. **Extension can't connect**:
   - Check server is running: `curl http://localhost:8765`
   - Verify extension configuration matches server ports
   - Check browser console for connection errors

3. **MCP client connection issues**:
   ```bash
   # Test MCP server
   curl http://localhost:3000/health

   # Check MCP server logs
   python server/server.py --debug
   ```

### Debug Mode

```bash
# Enable verbose logging
python server/server.py --debug

# Or set environment variable
export FOXMCP_DEBUG=1
python server/server.py
```

## Security Configuration

### Production Deployment

```python
# Production configuration
server = FoxMCPServer(
    host="localhost",      # Never use 0.0.0.0 in production
    enable_cors=False,     # Disable CORS for security
    require_auth=True,     # Enable authentication
    ssl_cert="cert.pem",   # Use SSL certificates
    ssl_key="key.pem"
)
```

### Development vs Production

```python
import os

# Environment-based configuration
if os.getenv("ENVIRONMENT") == "production":
    server = FoxMCPServer(
        host="localhost",
        enable_debug=False,
        require_auth=True
    )
else:
    server = FoxMCPServer(
        host="localhost",
        enable_debug=True,
        require_auth=False
    )
```

## Monitoring and Health Checks

### Health Endpoints

```bash
# Check WebSocket server
curl http://localhost:8765/health

# Check MCP server
curl http://localhost:3000/health

# Get server status
curl http://localhost:8765/status
```

### Metrics Collection

```python
# Enable metrics collection
server = FoxMCPServer(
    enable_metrics=True,
    metrics_port=9090
)

# Access metrics at http://localhost:9090/metrics
```
