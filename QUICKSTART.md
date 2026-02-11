# LACES-GENESIS OMNI - Quick Start Guide

## 🚀 Deploy in 5 Minutes

### Prerequisites
```bash
# Check requirements
docker --version          # Need 24.0+
docker-compose --version  # Need 2.20+
```

### Step 1: Clone & Configure
```bash
# Navigate to project directory
cd laces-genesis-omni

# Copy environment file
cp .env.example .env

# Edit .env with your credentials
nano .env
```

### Step 2: Start Services
```bash
# Start all services
docker-compose -f deployment/docker-compose.yml up -d

# Check status
docker-compose ps

# Should show:
# laces-database         running
# laces-redis            running
# laces-backend          running
# laces-maintenance-agent running
# laces-frontend         running
# laces-prometheus       running
# laces-grafana          running
```

### Step 3: Initialize Database
```bash
# Wait 30 seconds for database to be ready
sleep 30

# Initialize schema (already done by docker-entrypoint-initdb.d)
# Verify by checking tables
docker-compose exec database psql -U laces_admin -d laces_genesis -c "\dt"
```

### Step 4: Access Services
```bash
# Frontend (4D Ageing Simulator)
open http://localhost/4d-ageing-simulator.html

# API Documentation
open http://localhost:8000/docs

# Grafana Dashboard
open http://localhost:3000
# Login: admin / admin

# Prometheus Metrics
open http://localhost:9090
```

### Step 5: Create First User
```bash
# Register via API
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@laces.ai",
    "username": "admin",
    "password": "SecurePassword123!"
  }'

# Response will include access_token
```

### Step 6: Create First Digital Twin
```bash
# Save token from previous step
TOKEN="your_token_here"

# Create spindle twin
curl -X POST http://localhost:8000/twins \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Spindle #001",
    "twin_type": "spindle",
    "properties": {
      "max_rpm": 60000,
      "bearing_type": "ceramic",
      "material": "CFRP"
    }
  }'
```

### Step 7: Simulate Telemetry
```bash
# Ingest sample telemetry
curl -X POST http://localhost:8000/telemetry \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "550e8400-e29b-41d4-a716-446655440001",
    "rpm": 45000,
    "torque_nm": 35.2,
    "vibration_x_g": 0.12,
    "vibration_y_g": 0.15,
    "vibration_z_g": 0.09,
    "temperature_c": 78.5
  }'
```

---

## 🧪 Testing the System

### Load Test (1000 Users)
```bash
# Install Locust
pip install locust

# Run load test
locust -f tests/load/locustfile.py --headless -u 1000 -r 100 -t 2m --host http://localhost:8000
```

### Health Check
```bash
# Backend health
curl http://localhost:8000/health

# Expected response:
{
  "status": "healthy",
  "active_connections": 0,
  "database": "connected",
  "redis": "connected"
}
```

---

## 📊 Monitoring

### View Metrics
```bash
# Prometheus metrics
curl http://localhost:9090/api/v1/query?query=http_request_duration_seconds

# Grafana dashboards
# 1. Navigate to http://localhost:3000
# 2. Login (admin/admin)
# 3. Dashboards → LACES System Overview
```

### View Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f backend
docker-compose logs -f maintenance-agent
```

---

## 🛠️ Development Mode

### Run Backend Locally
```bash
# Install dependencies
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL=postgresql://laces_admin:changeme@localhost:5432/laces_genesis
export REDIS_URL=redis://localhost:6379

# Run with hot reload
uvicorn main:app --reload --port 8000
```

### Run Tests
```bash
cd backend
pytest tests/ -v --cov=. --cov-report=html
```

---

## 🔧 Troubleshooting

### Database Won't Start
```bash
# Check logs
docker-compose logs database

# Reset database
docker-compose down -v
docker-compose up -d database
```

### Backend Can't Connect to Database
```bash
# Verify database is ready
docker-compose exec database pg_isready -U laces_admin

# Check network
docker network inspect laces-genesis-omni_laces-network
```

### Port Already in Use
```bash
# Find process using port 8000
lsof -i :8000
kill -9 <PID>

# Or change ports in docker-compose.yml
ports:
  - "8001:8000"  # Change 8000 to 8001
```

---

## 📦 Production Deployment

### AWS Deployment
```bash
# 1. Push images to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag laces-backend:latest <account>.dkr.ecr.us-east-1.amazonaws.com/laces-backend:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/laces-backend:latest

# 2. Update ECS service
aws ecs update-service --cluster laces-cluster --service laces-backend --force-new-deployment
```

### Kubernetes Deployment
```bash
# Apply manifests
kubectl apply -f deployment/k8s/

# Check status
kubectl get pods -n laces
kubectl get svc -n laces
```

---

## 🔐 Security Checklist

- [ ] Change default passwords in .env
- [ ] Generate new SECRET_KEY (use `openssl rand -hex 32`)
- [ ] Enable SSL/TLS (configure Nginx)
- [ ] Set up firewall rules
- [ ] Enable backup automation
- [ ] Configure monitoring alerts
- [ ] Review RBAC permissions
- [ ] Enable audit logging

---

## 📚 Next Steps

1. **Read Full Documentation**: `docs/architecture.md`
2. **Explore API**: http://localhost:8000/docs
3. **Customize Dashboard**: Import Grafana dashboards from `deployment/grafana-dashboards/`
4. **Scale System**: `docker-compose up -d --scale backend=8`
5. **Enable HTTPS**: Configure SSL certificates in Nginx

---

## 🆘 Support

- **Documentation**: `README.md` and `docs/`
- **API Reference**: http://localhost:8000/docs
- **Issues**: GitHub Issues
- **Email**: support@laces.ai

---

**Ready to build the future of digital twins? Let's go! 🚀**
