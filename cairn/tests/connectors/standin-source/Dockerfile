# A stand-in source connector for Cairn's tests: it speaks the Airbyte protocol (spec, check, discover, read) and can
# be told to misbehave. Not a vendor's connector. It runs as a user that is not root, as most open connectors do.
FROM python:3.12-alpine
COPY source.py /connector/source.py
USER 10001:10001
ENTRYPOINT ["python", "-u", "/connector/source.py"]
