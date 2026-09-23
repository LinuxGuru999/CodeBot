# New Application Workflow

Last updated: 2026-09-20

## Overview

CodeBot can create new applications from natural language descriptions. This is one of the two primary entry points into the autonomous engineering system.

## Workflow

```
APPLICATION DESCRIPTION
        ↓
requirements extraction
        ↓
clarification / assumptions
        ↓
product specification
        ↓
feature model
        ↓
architecture
        ↓
technology decisions
        ↓
project structure
        ↓
engineering decomposition
        ↓
implementation
        ↓
testing
        ↓
QA
        ↓
documentation
        ↓
usable application
        ↓
continuous development
```

## Step 1: Application Description

The user describes what they want to build:

```text
Build a web application that lets small businesses
monitor computers, receive alerts, remotely diagnose
issues, and manage employees.
```

### Information Collected

- **Application name**
- **Application description**
- **Problem being solved**
- **Target users**
- **Primary workflows**
- **Required features**
- **Optional features**
- **Platforms**
- **Preferred technologies**
- **Technology restrictions**
- **Security requirements**
- **Performance requirements**
- **Deployment preferences**
- **External integrations**
- **Authentication requirements**
- **Data/storage requirements**
- **UI preferences**
- **Constraints**
- **Budget / compute constraints**
- **Priorities**

A simple natural-language description is sufficient to begin. CodeBot infers reasonable defaults and identifies important uncertainties.

## Step 2: Requirements Extraction

CodeBot transforms the description into structured requirements:

```text
Functional Requirements:
- Monitor computer health (CPU, memory, disk, network)
- Send alerts when thresholds exceeded
- Provide remote diagnostic tools
- Manage employee access and permissions

Non-Functional Requirements:
- Support 100+ monitored computers
- Real-time alerts (< 30 seconds)
- 99.9% uptime
- SOC 2 compliance

Constraints:
- Web-based interface
- Cloud deployment
- PostgreSQL database
```

## Step 3: Clarification

CodeBot identifies ambiguities and asks questions:

```text
"Should alerts be sent via email, SMS, or both?"
"What diagnostic tools do you need remotely?"
"Should employees have role-based access?"
"Do you need audit logging?"
```

## Step 4: Product Specification

Creates a persistent product specification:

```text
Vision: Small business computer monitoring platform
Goals: Real-time monitoring, alerting, remote diagnostics
Users: IT administrators, system managers
Features: Monitoring, Alerting, Diagnostics, Employee Management
Constraints: Web-based, cloud-hosted, PostgreSQL
```

## Step 5: Architecture Design

CodeBot designs the system architecture:

```text
┌─────────────────────────────────────────┐
│           Web Application               │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │Dashboard│  │ Alerts  │  │ Reports │ │
│  └─────────┘  └─────────┘  └─────────┘ │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│           API Server                    │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │ Monitor │  │  Alert  │  │   Auth  │ │
│  │ Service │  │ Service │  │ Service │ │
│  └─────────┘  └─────────┘  └─────────┘ │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│           Database                      │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │computers│  │ alerts  │  │  users  │ │
│  └─────────┘  └─────────┘  └─────────┘ │
└─────────────────────────────────────────┘
```

## Step 6: Technology Selection

CodeBot selects technologies based on:

- Application requirements
- Project size
- Platform
- Security
- Performance
- Maintainability
- Deployment target
- User preferences

Example selection:
- **Frontend**: React + TypeScript
- **Backend**: FastAPI + Python
- **Database**: PostgreSQL
- **Deployment**: Docker + AWS
- **Authentication**: JWT + OAuth2

## Step 7: Project Structure

CodeBot creates the repository structure:

```
my-project/
├── .codebot/
│   ├── project.yaml
│   ├── constitution.md
│   └── quality_gates.yaml
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── models/
│   │   ├── services/
│   │   └── auth/
│   ├── tests/
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   └── services/
│   ├── tests/
│   └── package.json
├── docker-compose.yml
├── Dockerfile
└── README.md
```

## Step 8: Implementation

CodeBot implements the application using the autonomous engineering pipeline:

1. Creates tickets for each feature
2. Plans implementation
3. Implements using TDD
4. Reviews with adversarial agents
5. Verifies with quality gates
6. Documents as it builds

## Step 9: Testing

CodeBot creates comprehensive tests:

- Unit tests for business logic
- Integration tests for API endpoints
- E2E tests for critical user flows
- Security tests for vulnerabilities
- Performance tests for scalability

## Step 10: Documentation

CodeBot generates documentation:

- README with setup instructions
- API documentation
- Architecture overview
- User guide
- Deployment guide
- Configuration reference

## Step 11: Continuous Development

After initial implementation, CodeBot continues to:

- Accept feature requests
- Handle revisions
- Fix bugs
- Improve performance
- Update dependencies
- Maintain documentation

## Example

User:
```text
Build a task management app with user authentication,
projects, tasks, and team collaboration.
```

CodeBot:
1. Extracts requirements for auth, projects, tasks, teams
2. Designs REST API architecture
3. Selects React + FastAPI + PostgreSQL
4. Creates repository structure
5. Implements authentication system
6. Implements project management
7. Implements task system
8. Adds real-time collaboration
9. Writes comprehensive tests
10. Generates full documentation
11. Deploys to cloud
