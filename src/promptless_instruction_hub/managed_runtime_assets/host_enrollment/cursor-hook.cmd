:; exec "${0%/*}/promptless-host-runtime" cursor-notify --detach --lifecycle "$@"
@echo off
"%~dp0promptless-host-runtime.exe" cursor-notify --detach --lifecycle %*
exit /b %ERRORLEVEL%
