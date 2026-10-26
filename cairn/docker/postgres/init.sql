-- Cairn control-plane database bootstrap (runs once on an empty pgvector/pgvector volume).
-- Tables are created by the services themselves (SQLAlchemy metadata.create_all / migrations).
SELECT 'CREATE DATABASE cairn'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cairn')\gexec

\connect cairn
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS cairn AUTHORIZATION CURRENT_USER;
