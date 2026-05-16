#!/usr/bin/env bash
# Build a multi-format DevOps fixture for end-to-end testing.
set -e

ROOT="${1:-/tmp/engram_fixture}"
rm -rf "$ROOT"
mkdir -p "$ROOT"/{services/payments,services/orders,infra/prod/k8s,infra/prod/chart,infra/staging}
cd "$ROOT"
git init -q .

cat > services/payments/Dockerfile <<'EOF'
FROM python:3.12-slim
ENV DATABASE_URL=postgres://localhost/db
ENV STRIPE_KEY=
EXPOSE 8080
COPY . /app
RUN pip install -r /app/requirements.txt
CMD ["python", "/app/main.py"]
EOF

cat > services/payments/main.py <<'EOF'
import os

def connect():
    return os.environ['DATABASE_URL']

def charge():
    key = os.environ.get('STRIPE_KEY')
    return key

class PaymentService:
    pass
EOF

cat > services/orders/Dockerfile <<'EOF'
FROM node:20-alpine
ENV DATABASE_URL=
ENV PAYMENTS_HOST=payments
EXPOSE 3000
EOF

cat > services/orders/server.js <<'EOF'
const db = process.env.DATABASE_URL;
const payments = process.env.PAYMENTS_HOST;

function start() {}

class OrderService {}

module.exports = { start, OrderService };
EOF

cat > infra/prod/main.tf <<'EOF'
provider "aws" {
  region = "us-east-1"
}

resource "aws_db_instance" "datatalks_prod_db" {
  identifier     = "datatalks-prod-db"
  engine         = "postgres"
  instance_class = "db.r5.large"
  tags = {
    Environment = "production"
  }
}

resource "aws_ecs_service" "payments" {
  name       = "payments"
  depends_on = [aws_db_instance.datatalks_prod_db]
}

resource "aws_ecs_service" "orders" {
  name       = "orders"
  depends_on = [aws_db_instance.datatalks_prod_db, aws_ecs_service.payments]
}
EOF

cat > infra/staging/main.tf <<'EOF'
resource "aws_db_instance" "staging_db" {
  identifier = "staging-db"
  engine     = "postgres"
  tags = {
    Environment = "staging"
  }
}
EOF

cat > infra/prod/k8s/payments-deployment.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
  namespace: prod
  labels:
    app: payments
    environment: production
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: payments
        image: payments:1.2.3
        ports:
        - containerPort: 8080
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: db-creds
              key: url
        - name: STRIPE_KEY
          valueFrom:
            secretKeyRef:
              name: stripe-creds
              key: key
EOF

cat > services/payments/docker-compose.yml <<'EOF'
services:
  payments:
    build: .
    environment:
      DATABASE_URL: postgres://db:5432/payments
      STRIPE_KEY: ${STRIPE_KEY}
    ports:
      - "8080:8080"
    depends_on:
      - db
  db:
    image: postgres:15
    environment:
      POSTGRES_PASSWORD: pg
EOF

cat > services/payments/Jenkinsfile <<'EOF'
pipeline {
  agent { docker { image 'python:3.12-slim' } }
  environment {
    DATABASE_URL = 'postgres://ci/db'
    STRIPE_KEY   = credentials('stripe-test')
  }
  stages {
    stage('Lint')  { steps { sh 'ruff check .' } }
    stage('Test')  { steps { sh 'pytest' } }
    stage('Build') { steps { sh 'docker build -t payments .' } }
    stage('Deploy Prod') { steps { sh 'kubectl apply -f infra/prod/k8s/' } }
  }
}
EOF

cat > infra/prod/chart/Chart.yaml <<'EOF'
apiVersion: v2
name: payments-chart
version: 1.0.0
appVersion: 1.2.3
type: application
dependencies:
  - name: postgresql
    version: 12.0.0
    repository: https://charts.bitnami.com/bitnami
EOF

cat > services/payments/.env.production <<'EOF'
DATABASE_URL=postgres://prod-db:5432/payments
STRIPE_KEY=sk_live_xxxxx
LOG_LEVEL=INFO
EOF

echo "Fixture built at $ROOT"
find "$ROOT" -type f -not -path '*/.git/*' | sort
