# OpenRouter Proxy

A simple proxy server for OpenRouter API that helps bypass rate limits on free API keys
by rotating through multiple API keys in a round-robin fashion.

## Features

- Proxies all requests to OpenRouter API v1
- Rotates multiple API keys to bypass rate limits
- Automatically disables API keys temporarily when rate limits are reached
- Streams responses chunk by chunk for efficient data transfer
- Simple authentication for accessing the proxy
- Uses OpenAI SDK for compatible endpoints for reliable handling
- Theoretically compatible with any OpenAI-compatible API by changing the `base_url` and `public_endpoints` in `config.yml`

## Setup

1. Clone the repository
2. Create a virtual environment and install dependencies:
    ```
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    pip install -r requirements.txt
    ```
3. Create a configuration file:
    ```
    cp config.yml.example config.yml
    ```
4. Edit `config.yml` to add your OpenRouter API keys and configure the server

## Configuration

The `config.yml` file supports the following settings:

```yaml
# Server settings
server:
  host: "0.0.0.0"  # Interface to bind to
  port: 5555       # Port to listen on
  access_key: "your_local_access_key_here"  # Authentication key
  log_level: "INFO"  # Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
  http_log_level: "INFO"  # HTTP access logs level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

# OpenRouter API keys
openrouter:
  keys:
    - "sk-or-v1-your-first-api-key"
    - "sk-or-v1-your-second-api-key"
    - "sk-or-v1-your-third-api-key"

  # Key selection strategy: "round-robin" (default), "first" or "random".
  key_selection_strategy: "round-robin"
  # List of key selection options:
  #   "same": Always use the last used key as long as it is possible.
  key_selection_opts: []

  # OpenRouter API base URL
  base_url: "https://openrouter.ai/api/v1"

  # Public endpoints that don't require authentication
  public_endpoints:
    - "/api/v1/models"

  # Time in seconds to temporarily disable a key when rate limit is reached by default
  rate_limit_cooldown: 14400  # 4 hours
  free_only: false # try to show only free models
  # OpenRouter can return a 429 error if a model is overloaded.
  # Additionally, Google sometimes returns 429 RESOURCE_EXHAUSTED errors repeatedly,
  # which can cause Roo Code to stop.
  # This option prevents repeated failures by introducing a delay before retrying.
  # global_rate_delay: 10 # in seconds
  global_rate_delay: 0

# Proxy settings for outgoing requests to OpenRouter
requestProxy:
  enabled: false    # Set to true to enable proxy
  url: "socks5://username:password@example.com:1080"  # Proxy URL with optional credentials embedded
```

## Usage

### Running Manually

Start the server:
```
python main.py
```

The proxy will be available at `http://localhost:5555/api/v1` (or the host/port configured in your config file).

### Installing as a Systemd Service

For Linux systems with systemd, you can install the proxy as a system service:

1. Make sure you've created and configured your `config.yml` file
2. Run the installation script:

```sudo ./service_install.sh``` or ```sudo ./service_install_venv.sh``` for venv.

This will create a systemd service that starts automatically on boot.

To check the service status:
```
sudo systemctl status openrouter-proxy
```

To view logs:
```
sudo journalctl -u openrouter-proxy -f
```

To uninstall the service:
```
sudo ./service_uninstall.sh
```

### Authentication

Add your local access key to requests:
```
Authorization: Bearer your_local_access_key_here
```

## API Endpoints

The proxy supports all OpenRouter API v1 endpoints through the following endpoint:

- `/api/v1/{path}` - Proxies all requests to OpenRouter API v1

It also provides a health check endpoint:

- `/health` - Health check endpoint that returns `{"status": "ok"}`
