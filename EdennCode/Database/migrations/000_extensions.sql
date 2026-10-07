-- Migration 000: enable required extensions.
-- Prereq: azure.extensions server parameter must include 'vector' (Task 1, Step 4).
CREATE EXTENSION IF NOT EXISTS vector;
