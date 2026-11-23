python -m venv "$env:RUNNER_TEMP/app-venv"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$py = "$env:RUNNER_TEMP/app-venv/Scripts/python.exe"
& $py -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m pip install --require-hashes -r requirements.lock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
