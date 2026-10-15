FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pilot ./pilot
COPY services ./services
COPY --from=frontend /build/dist ./frontend/dist
RUN useradd -m app && mkdir /app/data && chown app:app /app/data
USER app
EXPOSE 8950
CMD ["uvicorn","pilot.app:app","--host","0.0.0.0","--port","8950"]
