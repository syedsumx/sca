-- Aegis control-plane database bootstrap (runs once on an empty pgvector/pgvector volume).
-- Tables are created by the services themselves (SQLAlchemy metadata.create_all / migrations).
SELECT 'CREATE DATABASE aegis'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'aegis')\gexec

\connect aegis
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS aegis AUTHORIZATION CURRENT_USER;
