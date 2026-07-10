@echo off
rem Windows wrapper for the tvly thin client.
rem Place this tvly.cmd on PATH (e.g. in %USERPROFILE%\bin or a tools dir).
rem Point TVLY_CLIENT_SCRIPT at the tvly python script (client/tvly), or edit below.
rem
rem Env used by the python script:
rem   TAVILY_GATEWAY_URL     e.g. http://gateway-host:18790
rem   TAVILY_GATEWAY_TOKEN   bearer token (must match the gateway)
rem   TAVILY_CLIENT_TIMEOUT  HTTP timeout seconds (default 600)

if "%TVLY_CLIENT_SCRIPT%"=="" (
  if exist "%~dp0tvly" (
    set "TVLY_CLIENT_SCRIPT=%~dp0tvly"
  ) else (
    echo tvly: TVLY_CLIENT_SCRIPT not set and no 'tvly' script next to tvly.cmd 1>&2
    exit /b 1
  )
)

python "%TVLY_CLIENT_SCRIPT%" %*
exit /b %ERRORLEVEL%