-- DHE control-plane database bootstrap (runs once on an empty pgvector/pgvector volume).
-- Tables are created by the services themselves (SQLAlchemy metadata.create_all / migrations).
SELECT 'CREATE DATABASE dhe'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dhe')\gexec

\connect dhe
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS dhe AUTHORIZATION CURRENT_USER;
