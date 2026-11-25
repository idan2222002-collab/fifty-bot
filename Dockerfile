FROM python:3.12-slim
WORKDIR /app
COPY *.py *.md ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "guard.py", "dry"]
