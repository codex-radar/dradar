# Credential-free AGY cancellation integration

Build `docker build -t dradar-0126-fixture -f tests/fixtures/antigravity-cancel.Dockerfile .`.
Run the real Linux supervisor/process/git tests with:

```
docker run --rm --network none -v "$PWD:/source:ro" -w /source dradar-0126-fixture sh -c 'PYTHONPATH=src python3 tests/test_antigravity_runtime.py'
```

With Pier installed in the selected Python interpreter:

```
DRADAR_AGY_TEST_OUTPUT=/absolute/private/evidence LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=src:tests python tests/agy_pier_integration.py
```

The matrix uses real Trial, DockerEnvironment, agent run, collection and cleanup.
FixtureAGY substitutes only installation/OAuth preparation and the executable;
no credentials, task claims or model/API calls. Inference networking is disabled.
Modes: normal, nonzero, empty, cancel, repeated cancel, export failure, supervisor
crash and hostile Git filter. Accepted results must match the host-only receipt,
run identity and digest; failure/crash must not pass that gate. Unit coverage
separately injects Git timeout and unconfirmed writer shutdown. Fixture containers
are removed by Pier. Logs and result paths remain in the supplied evidence folder.
