: <<'WINDOWS_BATCH'
@echo off
"%~dp0promptless-host-runtime.exe" cursor-hook --lifecycle %1
exit /b %errorlevel%
WINDOWS_BATCH
runtime_dir=${0%/*}
exec "$runtime_dir/promptless-host-runtime" cursor-hook --lifecycle "$1"
