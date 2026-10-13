FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
USER 65532:65532
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
