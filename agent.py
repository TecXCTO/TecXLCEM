# LACES Maintenance Agent - AI-Powered Fleet Management
# Monitors 1000+ nodes, predicts failures, optimizes maintenance schedules

import asyncio
import asyncpg
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import json
import logging
from scipy import stats
from sklearn.ensemble import IsolationForest
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

class AgentConfig:
    DATABASE_URL = "postgresql://laces_admin:password@localhost:5432/laces_genesis"
    SMTP_SERVER = "smtp.example.com"
    SMTP_PORT = 587
    ALERT_EMAIL = "maintenance@laces.ai"
    
    # Thresholds
    VIBRATION_CRITICAL = 0.8  # g
    VIBRATION_WARNING = 0.5   # g
    TEMPERATURE_CRITICAL = 95 # °C
    TEMPERATURE_WARNING = 85  # °C
    TOOL_WEAR_CRITICAL = 80   # %
    TOOL_WEAR_WARNING = 60    # %
    
    # Scheduling
    CHECK_INTERVAL = 60       # seconds
    ANOMALY_WINDOW = 3600     # 1 hour of data for anomaly detection
    PREDICTION_HORIZON = 720  # hours (30 days)

config = AgentConfig()

# ============================================================================
# DATA MODELS
# ============================================================================

class Severity(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

@dataclass
class NodeHealth:
    node_id: str
    status: str
    health_score: float  # 0-100
    vibration_level: float
    temperature: float
    rpm: float
    tool_wear: float
    last_maintenance_days: int
    predicted_failure_hours: Optional[float]
    anomaly_score: float

@dataclass
class MaintenanceRecommendation:
    node_id: str
    severity: Severity
    issue_type: str
    description: str
    recommended_action: str
    urgency_hours: int
    estimated_cost: float
    estimated_downtime_hours: float
    parts_needed: List[str]

# ============================================================================
# MAINTENANCE AGENT
# ============================================================================

class MaintenanceAgent:
    """
    AI-powered maintenance agent that:
    1. Monitors telemetry from 1000+ nodes in real-time
    2. Detects anomalies using ML (Isolation Forest)
    3. Predicts failures using physics-based models + ML
    4. Generates maintenance tickets automatically
    5. Optimizes maintenance schedules across fleet
    6. Sends alerts to operators
    """
    
    def __init__(self):
        self.db_pool: Optional[asyncpg.Pool] = None
        self.anomaly_detectors: Dict[str, IsolationForest] = {}
        self.node_health_cache: Dict[str, NodeHealth] = {}
        self.running = False
        
    async def start(self):
        """Initialize and start the maintenance agent."""
        logger.info("Starting LACES Maintenance Agent...")
        
        # Connect to database
        self.db_pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=5,
            max_size=20
        )
        
        # Train initial anomaly detectors
        await self.train_anomaly_detectors()
        
        # Start monitoring loops
        self.running = True
        await asyncio.gather(
            self.telemetry_monitor_loop(),
            self.predictive_maintenance_loop(),
            self.schedule_optimizer_loop(),
            self.alert_dispatcher_loop()
        )
    
    async def stop(self):
        """Gracefully stop the agent."""
        self.running = False
        if self.db_pool:
            await self.db_pool.close()
        logger.info("Maintenance Agent stopped.")
    
    # ========================================================================
    # TELEMETRY MONITORING
    # ========================================================================
    
    async def telemetry_monitor_loop(self):
        """Continuously monitor telemetry data for anomalies and threshold violations."""
        logger.info("Telemetry monitor started")
        
        while self.running:
            try:
                # Get all active nodes
                nodes = await self.db_pool.fetch(
                    "SELECT node_id FROM machine_nodes WHERE status = 'online'"
                )
                
                # Check each node
                for node in nodes:
                    node_id = node['node_id']
                    health = await self.assess_node_health(node_id)
                    self.node_health_cache[str(node_id)] = health
                    
                    # Check for critical conditions
                    await self.check_thresholds(health)
                    await self.detect_anomalies(health)
                
                logger.info(f"Monitored {len(nodes)} nodes")
                
            except Exception as e:
                logger.error(f"Error in telemetry monitor: {e}")
            
            await asyncio.sleep(config.CHECK_INTERVAL)
    
    async def assess_node_health(self, node_id: str) -> NodeHealth:
        """Assess overall health of a node based on recent telemetry."""
        
        # Get recent telemetry (last 5 minutes)
        telemetry = await self.db_pool.fetch(
            """
            SELECT * FROM telemetry_data
            WHERE node_id = $1 AND time > NOW() - INTERVAL '5 minutes'
            ORDER BY time DESC
            LIMIT 100
            """,
            node_id
        )
        
        if not telemetry:
            return NodeHealth(
                node_id=str(node_id),
                status="unknown",
                health_score=0.0,
                vibration_level=0.0,
                temperature=0.0,
                rpm=0.0,
                tool_wear=0.0,
                last_maintenance_days=9999,
                predicted_failure_hours=None,
                anomaly_score=0.0
            )
        
        # Calculate averages
        avg_vibration = np.mean([
            np.sqrt(t['vibration_x_g']**2 + t['vibration_y_g']**2 + t['vibration_z_g']**2)
            for t in telemetry if all([t['vibration_x_g'], t['vibration_y_g'], t['vibration_z_g']])
        ]) if len(telemetry) > 0 else 0.0
        
        avg_temp = np.mean([t['temperature_c'] for t in telemetry if t['temperature_c']]) if len(telemetry) > 0 else 0.0
        avg_rpm = np.mean([t['rpm'] for t in telemetry if t['rpm']]) if len(telemetry) > 0 else 0.0
        avg_tool_wear = np.mean([t['tool_wear_percent'] for t in telemetry if t['tool_wear_percent']]) if len(telemetry) > 0 else 0.0
        
        # Get maintenance history
        node_info = await self.db_pool.fetchrow(
            "SELECT last_maintenance_date FROM machine_nodes WHERE node_id = $1",
            node_id
        )
        
        days_since_maintenance = 0
        if node_info and node_info['last_maintenance_date']:
            days_since_maintenance = (datetime.now().date() - node_info['last_maintenance_date']).days
        
        # Calculate health score (0-100)
        health_score = self.calculate_health_score(
            avg_vibration, avg_temp, avg_rpm, avg_tool_wear, days_since_maintenance
        )
        
        # Predict failure
        predicted_failure = await self.predict_failure_time(node_id, telemetry)
        
        return NodeHealth(
            node_id=str(node_id),
            status="online",
            health_score=health_score,
            vibration_level=avg_vibration,
            temperature=avg_temp,
            rpm=avg_rpm,
            tool_wear=avg_tool_wear,
            last_maintenance_days=days_since_maintenance,
            predicted_failure_hours=predicted_failure,
            anomaly_score=0.0  # Will be calculated separately
        )
    
    def calculate_health_score(
        self,
        vibration: float,
        temperature: float,
        rpm: float,
        tool_wear: float,
        days_since_maintenance: int
    ) -> float:
        """Calculate a composite health score (0-100, higher is better)."""
        
        # Vibration score (inverse)
        vib_score = max(0, 100 - (vibration / config.VIBRATION_CRITICAL) * 100)
        
        # Temperature score (inverse)
        temp_score = max(0, 100 - (temperature / config.TEMPERATURE_CRITICAL) * 100)
        
        # Tool wear score (inverse)
        wear_score = max(0, 100 - tool_wear)
        
        # Maintenance freshness score (inverse exponential)
        maint_score = 100 * np.exp(-days_since_maintenance / 180)  # Half-life of 180 days
        
        # Weighted average
        health_score = (
            0.3 * vib_score +
            0.25 * temp_score +
            0.25 * wear_score +
            0.2 * maint_score
        )
        
        return max(0.0, min(100.0, health_score))
    
    async def check_thresholds(self, health: NodeHealth):
        """Check if any metrics exceed critical/warning thresholds."""
        
        violations = []
        
        # Vibration
        if health.vibration_level >= config.VIBRATION_CRITICAL:
            violations.append({
                "type": "vibration_critical",
                "severity": Severity.CRITICAL,
                "message": f"Critical vibration: {health.vibration_level:.2f}g (limit: {config.VIBRATION_CRITICAL}g)"
            })
        elif health.vibration_level >= config.VIBRATION_WARNING:
            violations.append({
                "type": "vibration_warning",
                "severity": Severity.HIGH,
                "message": f"High vibration: {health.vibration_level:.2f}g (limit: {config.VIBRATION_WARNING}g)"
            })
        
        # Temperature
        if health.temperature >= config.TEMPERATURE_CRITICAL:
            violations.append({
                "type": "temperature_critical",
                "severity": Severity.CRITICAL,
                "message": f"Critical temperature: {health.temperature:.1f}°C (limit: {config.TEMPERATURE_CRITICAL}°C)"
            })
        elif health.temperature >= config.TEMPERATURE_WARNING:
            violations.append({
                "type": "temperature_warning",
                "severity": Severity.HIGH,
                "message": f"High temperature: {health.temperature:.1f}°C (limit: {config.TEMPERATURE_WARNING}°C)"
            })
        
        # Tool wear
        if health.tool_wear >= config.TOOL_WEAR_CRITICAL:
            violations.append({
                "type": "tool_wear_critical",
                "severity": Severity.CRITICAL,
                "message": f"Critical tool wear: {health.tool_wear:.1f}% (limit: {config.TOOL_WEAR_CRITICAL}%)"
            })
        elif health.tool_wear >= config.TOOL_WEAR_WARNING:
            violations.append({
                "type": "tool_wear_warning",
                "severity": Severity.MEDIUM,
                "message": f"High tool wear: {health.tool_wear:.1f}% (limit: {config.TOOL_WEAR_WARNING}%)"
            })
        
        # Create tickets for violations
        for violation in violations:
            await self.create_maintenance_ticket(
                node_id=health.node_id,
                severity=violation["severity"],
                title=violation["message"],
                description=f"Automatic threshold violation detected. Health score: {health.health_score:.1f}",
                diagnostic_data={
                    "vibration_g": health.vibration_level,
                    "temperature_c": health.temperature,
                    "rpm": health.rpm,
                    "tool_wear_percent": health.tool_wear
                }
            )
    
    # ========================================================================
    # ANOMALY DETECTION
    # ========================================================================
    
    async def train_anomaly_detectors(self):
        """Train Isolation Forest models for each node using historical data."""
        logger.info("Training anomaly detectors...")
        
        nodes = await self.db_pool.fetch(
            "SELECT DISTINCT node_id FROM telemetry_data"
        )
        
        for node in nodes:
            node_id = str(node['node_id'])
            
            # Get training data (last 30 days, healthy operation)
            data = await self.db_pool.fetch(
                """
                SELECT rpm, torque_nm, vibration_x_g, vibration_y_g, vibration_z_g,
                       temperature_c, power_consumption_w
                FROM telemetry_data
                WHERE node_id = $1 
                  AND time > NOW() - INTERVAL '30 days'
                  AND error_code IS NULL
                ORDER BY time DESC
                LIMIT 10000
                """,
                node['node_id']
            )
            
            if len(data) < 100:
                continue
            
            # Convert to numpy array
            X = np.array([
                [
                    d['rpm'] or 0,
                    d['torque_nm'] or 0,
                    d['vibration_x_g'] or 0,
                    d['vibration_y_g'] or 0,
                    d['vibration_z_g'] or 0,
                    d['temperature_c'] or 0,
                    d['power_consumption_w'] or 0
                ]
                for d in data
            ])
            
            # Train Isolation Forest
            clf = IsolationForest(
                contamination=0.05,  # Expect 5% anomalies
                random_state=42,
                n_estimators=100
            )
            clf.fit(X)
            
            self.anomaly_detectors[node_id] = clf
        
        logger.info(f"Trained {len(self.anomaly_detectors)} anomaly detectors")
    
    async def detect_anomalies(self, health: NodeHealth):
        """Detect anomalies in node behavior using trained ML model."""
        
        if health.node_id not in self.anomaly_detectors:
            return
        
        # Get recent data point
        recent = await self.db_pool.fetchrow(
            """
            SELECT * FROM telemetry_data
            WHERE node_id = $1
            ORDER BY time DESC
            LIMIT 1
            """,
            health.node_id
        )
        
        if not recent:
            return
        
        # Create feature vector
        X = np.array([[
            recent['rpm'] or 0,
            recent['torque_nm'] or 0,
            recent['vibration_x_g'] or 0,
            recent['vibration_y_g'] or 0,
            recent['vibration_z_g'] or 0,
            recent['temperature_c'] or 0,
            recent['power_consumption_w'] or 0
        ]])
        
        # Predict anomaly
        clf = self.anomaly_detectors[health.node_id]
        prediction = clf.predict(X)[0]  # -1 for anomaly, 1 for normal
        anomaly_score = -clf.score_samples(X)[0]  # Higher = more anomalous
        
        if prediction == -1:
            await self.create_maintenance_ticket(
                node_id=health.node_id,
                severity=Severity.HIGH,
                title="Anomalous behavior detected",
                description=f"Machine learning model detected abnormal operation pattern. Anomaly score: {anomaly_score:.3f}",
                diagnostic_data={
                    "anomaly_score": float(anomaly_score),
                    "rpm": float(recent['rpm']) if recent['rpm'] else None,
                    "temperature": float(recent['temperature_c']) if recent['temperature_c'] else None
                }
            )
    
    # ========================================================================
    # PREDICTIVE MAINTENANCE
    # ========================================================================
    
    async def predictive_maintenance_loop(self):
        """Run predictive maintenance models periodically."""
        logger.info("Predictive maintenance loop started")
        
        while self.running:
            try:
                # Get nodes with sufficient data
                nodes = await self.db_pool.fetch(
                    """
                    SELECT DISTINCT node_id 
                    FROM telemetry_data
                    WHERE time > NOW() - INTERVAL '7 days'
                    """
                )
                
                for node in nodes:
                    node_id = str(node['node_id'])
                    
                    # Run prediction
                    prediction = await self.run_predictive_model(node_id)
                    
                    if prediction and prediction['failure_probability'] > 0.7:
                        # High probability of failure soon
                        await self.create_maintenance_ticket(
                            node_id=node_id,
                            severity=Severity.CRITICAL,
                            title="Predicted failure imminent",
                            description=f"Predictive model indicates {prediction['failure_probability']*100:.1f}% probability of failure within {prediction['time_to_failure_hours']:.0f} hours",
                            diagnostic_data=prediction
                        )
                
            except Exception as e:
                logger.error(f"Error in predictive maintenance: {e}")
            
            await asyncio.sleep(3600)  # Run every hour
    
    async def run_predictive_model(self, node_id: str) -> Optional[Dict]:
        """Run physics-based + ML predictive model for a node."""
        
        # Get recent telemetry
        data = await self.db_pool.fetch(
            """
            SELECT * FROM telemetry_data
            WHERE node_id = $1 AND time > NOW() - INTERVAL '7 days'
            ORDER BY time ASC
            """,
            node_id
        )
        
        if len(data) < 100:
            return None
        
        # Extract features
        vibrations = [
            np.sqrt(d['vibration_x_g']**2 + d['vibration_y_g']**2 + d['vibration_z_g']**2)
            for d in data if all([d['vibration_x_g'], d['vibration_y_g'], d['vibration_z_g']])
        ]
        temperatures = [d['temperature_c'] for d in data if d['temperature_c']]
        
        if not vibrations or not temperatures:
            return None
        
        # Calculate trend (linear regression)
        t = np.arange(len(vibrations))
        vib_trend = stats.linregress(t, vibrations).slope if len(vibrations) > 1 else 0
        temp_trend = stats.linregress(t, temperatures).slope if len(temperatures) > 1 else 0
        
        # Simple failure prediction based on trends
        # If vibration is increasing rapidly, predict failure
        current_vib = np.mean(vibrations[-10:])
        vib_rate = vib_trend * 24  # Per day
        
        if vib_rate > 0.01:  # Increasing
            # Time until critical threshold
            hours_to_failure = (config.VIBRATION_CRITICAL - current_vib) / (vib_rate / 24)
            failure_prob = min(1.0, current_vib / config.VIBRATION_CRITICAL)
        else:
            hours_to_failure = None
            failure_prob = 0.1
        
        return {
            "node_id": node_id,
            "time_to_failure_hours": hours_to_failure,
            "failure_probability": failure_prob,
            "vibration_trend": vib_trend,
            "temperature_trend": temp_trend,
            "current_vibration": current_vib
        }
    
    async def predict_failure_time(self, node_id: str, recent_data: List) -> Optional[float]:
        """Simplified failure prediction for health assessment."""
        if not recent_data:
            return None
        
        vibrations = [
            np.sqrt(d['vibration_x_g']**2 + d['vibration_y_g']**2 + d['vibration_z_g']**2)
            for d in recent_data if all([d['vibration_x_g'], d['vibration_y_g'], d['vibration_z_g']])
        ]
        
        if len(vibrations) < 2:
            return None
        
        avg_vib = np.mean(vibrations)
        if avg_vib >= config.VIBRATION_CRITICAL:
            return 0.0  # Immediate failure risk
        
        # Exponential decay model
        hours = 720 * (1 - avg_vib / config.VIBRATION_CRITICAL)
        return max(0.0, hours)
    
    # ========================================================================
    # MAINTENANCE SCHEDULING
    # ========================================================================
    
    async def schedule_optimizer_loop(self):
        """Optimize maintenance schedules across the fleet."""
        logger.info("Schedule optimizer started")
        
        while self.running:
            try:
                # Get all open maintenance tickets
                tickets = await self.db_pool.fetch(
                    """
                    SELECT * FROM maintenance_tickets
                    WHERE status IN ('open', 'acknowledged')
                    ORDER BY severity DESC, created_at ASC
                    """
                )
                
                # Prioritize by severity and impact
                prioritized = self.prioritize_maintenance(tickets)
                
                # Generate recommendations
                for ticket in prioritized[:10]:  # Top 10
                    recommendation = await self.generate_recommendation(ticket)
                    logger.info(f"Recommendation for {ticket['node_id']}: {recommendation.recommended_action}")
                
            except Exception as e:
                logger.error(f"Error in schedule optimizer: {e}")
            
            await asyncio.sleep(1800)  # Run every 30 minutes
    
    def prioritize_maintenance(self, tickets: List) -> List:
        """Prioritize maintenance tickets based on multiple factors."""
        
        severity_weights = {
            'critical': 100,
            'high': 75,
            'medium': 50,
            'low': 25
        }
        
        scored = []
        for ticket in tickets:
            score = severity_weights.get(ticket['severity'], 0)
            
            # Boost score if ticket is old
            age_hours = (datetime.now() - ticket['created_at']).total_seconds() / 3600
            score += age_hours * 0.5
            
            scored.append((score, ticket))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ticket for score, ticket in scored]
    
    async def generate_recommendation(self, ticket: Dict) -> MaintenanceRecommendation:
        """Generate detailed maintenance recommendation."""
        
        # Get node health
        node_id = str(ticket['node_id'])
        health = self.node_health_cache.get(node_id)
        
        if not health:
            health = await self.assess_node_health(ticket['node_id'])
        
        # Determine action based on issue
        if health.tool_wear >= 80:
            action = "Replace cutting tool immediately"
            parts = ["Cutting Tool Assembly"]
            cost = 450.0
            downtime = 2.0
        elif health.vibration_level >= config.VIBRATION_CRITICAL:
            action = "Inspect and replace bearings"
            parts = ["Front Bearing Set", "Rear Bearing Set"]
            cost = 1200.0
            downtime = 8.0
        elif health.temperature >= config.TEMPERATURE_CRITICAL:
            action = "Check cooling system, replace thermal compound"
            parts = ["Thermal Compound", "Coolant"]
            cost = 150.0
            downtime = 3.0
        else:
            action = "Perform routine inspection and lubrication"
            parts = ["Lubricant", "Filter Kit"]
            cost = 80.0
            downtime = 1.5
        
        urgency = 24 if ticket['severity'] == 'critical' else 168  # 1 day or 1 week
        
        return MaintenanceRecommendation(
            node_id=node_id,
            severity=Severity(ticket['severity']),
            issue_type=ticket['title'],
            description=ticket['description'] or "",
            recommended_action=action,
            urgency_hours=urgency,
            estimated_cost=cost,
            estimated_downtime_hours=downtime,
            parts_needed=parts
        )
    
    # ========================================================================
    # TICKET MANAGEMENT
    # ========================================================================
    
    async def create_maintenance_ticket(
        self,
        node_id: str,
        severity: Severity,
        title: str,
        description: str,
        diagnostic_data: Dict
    ):
        """Create a new maintenance ticket if it doesn't already exist."""
        
        # Check for duplicate (same node, same type, within 24 hours)
        existing = await self.db_pool.fetchrow(
            """
            SELECT ticket_id FROM maintenance_tickets
            WHERE node_id = $1 
              AND title = $2
              AND status IN ('open', 'acknowledged')
              AND created_at > NOW() - INTERVAL '24 hours'
            """,
            node_id, title
        )
        
        if existing:
            logger.debug(f"Duplicate ticket suppressed for {node_id}: {title}")
            return
        
        # Create ticket
        await self.db_pool.execute(
            """
            INSERT INTO maintenance_tickets
            (node_id, severity, title, description, diagnostic_data)
            VALUES ($1, $2, $3, $4, $5)
            """,
            node_id, severity.value, title, description, json.dumps(diagnostic_data)
        )
        
        logger.info(f"Created {severity.value} ticket for {node_id}: {title}")
    
    # ========================================================================
    # ALERTING
    # ========================================================================
    
    async def alert_dispatcher_loop(self):
        """Dispatch alerts for critical/high severity tickets."""
        logger.info("Alert dispatcher started")
        
        while self.running:
            try:
                # Get unacknowledged critical tickets
                tickets = await self.db_pool.fetch(
                    """
                    SELECT * FROM maintenance_tickets
                    WHERE severity IN ('critical', 'high')
                      AND status = 'open'
                      AND created_at > NOW() - INTERVAL '1 hour'
                    """
                )
                
                for ticket in tickets:
                    await self.send_alert(ticket)
                    
                    # Mark as acknowledged
                    await self.db_pool.execute(
                        """
                        UPDATE maintenance_tickets
                        SET status = 'acknowledged', acknowledged_at = NOW()
                        WHERE ticket_id = $1
                        """,
                        ticket['ticket_id']
                    )
                
            except Exception as e:
                logger.error(f"Error in alert dispatcher: {e}")
            
            await asyncio.sleep(300)  # Check every 5 minutes
    
    async def send_alert(self, ticket: Dict):
        """Send email/SMS alert for a maintenance ticket."""
        
        subject = f"[{ticket['severity'].upper()}] LACES Maintenance Alert: {ticket['title']}"
        
        body = f"""
        LACES Maintenance Alert
        
        Node ID: {ticket['node_id']}
        Severity: {ticket['severity']}
        Issue: {ticket['title']}
        
        Description:
        {ticket['description']}
        
        Created: {ticket['created_at']}
        
        Please investigate immediately if severity is CRITICAL.
        
        --
        LACES Maintenance Agent
        """
        
        logger.info(f"Sending alert: {subject}")
        # In production, actually send email here
        # For now, just log
        
        # Example SMTP code (commented out):
        # msg = MIMEMultipart()
        # msg['From'] = config.ALERT_EMAIL
        # msg['To'] = config.ALERT_EMAIL
        # msg['Subject'] = subject
        # msg.attach(MIMEText(body, 'plain'))
        # with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
        #     server.starttls()
        #     server.send_message(msg)

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

async def main():
    agent = MaintenanceAgent()
    try:
        await agent.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await agent.stop()

if __name__ == "__main__":
    asyncio.run(main())
