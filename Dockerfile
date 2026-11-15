FROM python:3.12-slim

WORKDIR /app

# Run as non-root user for container security
RUN useradd -m -u 1000 probeuser

COPY src/ /app/src/
COPY tests/ /app/tests/
COPY data/ /app/data/
COPY README.md REPORT.md /app/

RUN chown -R probeuser:probeuser /app
USER probeuser

ENV PYTHONUNBUFFERED=1
ENV SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"

# Default entrypoint runs unit tests, executes the dry-run + Mainnet block probe, and verifies on-chain signatures
ENTRYPOINT ["python3", "src/probe.py"]
CMD ["--max-spend-sol", "0.02", "--blocks-per-validator", "2"]
