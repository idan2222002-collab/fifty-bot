FROM python:3.12-slim
WORKDIR /app
COPY *.py *.md ./
ENV PYTHONUNBUFFERED=1 SHACHAR_DATA=/data
VOLUME /data
RUN python -m unittest test_shachar
CMD ["python", "guard.py", "dry"]
