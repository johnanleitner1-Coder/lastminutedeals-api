FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir mcp httpx uvicorn python-dotenv

COPY tools/run_mcp_remote.py tools/run_mcp_remote.py

ENV BOOKING_API_URL=https://web-production-dc74b.up.railway.app
ENV PORT=8080

EXPOSE 8080

CMD ["python", "tools/run_mcp_remote.py"]
