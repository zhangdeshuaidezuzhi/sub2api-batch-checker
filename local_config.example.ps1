# Copy this file to local_config.ps1 for your own machine.
# local_config.ps1 is ignored by git because it may contain private paths.

$env:SUB2API_CHECKER_DEFAULT_INPUT = "D:\your-token-folder"
$env:SUB2API_CHECKER_PROXY = "http://127.0.0.1:7897"
$env:SUB2API_CLOUD_SSH_KEY = "C:\path\to\your\sub2api_key"
$env:SUB2API_CLOUD_SSH_TARGET = "root@your-server"
