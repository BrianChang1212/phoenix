@echo off
if not defined PHOENIX_PNPM_NODE (
  echo PHOENIX_PNPM_NODE must name the compatible node.exe. 1>&2
  exit /b 2
)
if not defined PHOENIX_PNPM_CLI (
  echo PHOENIX_PNPM_CLI must name the JavaScript pnpm launcher. 1>&2
  exit /b 2
)
"%PHOENIX_PNPM_NODE%" "%PHOENIX_PNPM_CLI%" --pm-on-fail=ignore --config.manage-package-manager-versions=false --config.verify-deps-before-run=false --config.enable-pre-post-scripts=false %*
exit /b %errorlevel%
