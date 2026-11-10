FROM python:slim

COPY . /app

WORKDIR /app

RUN python3 -m pip install -r requirements.txt

CMD ["uvicorn", "--host", "0.0.0.0", "--port", "17888", "main:app"]
