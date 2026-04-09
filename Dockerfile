FROM python:3.12-slim

WORKDIR /app

RUN apt-get update; apt-get install -y git
COPY ./src/requirements.txt .
RUN pip install --no-cache-dir -r ./requirements.txt

COPY ./src .
CMD [ "python", "-u", "./main.py" ]
