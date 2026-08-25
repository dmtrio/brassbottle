FROM python:3.12-alpine

RUN apk add --no-cache restic

COPY src/backup_service.py /usr/local/lib/djinn/backup_service.py

ENTRYPOINT ["python3", "/usr/local/lib/djinn/backup_service.py"]
