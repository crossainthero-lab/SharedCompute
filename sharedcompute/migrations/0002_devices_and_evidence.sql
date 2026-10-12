-- SharedCompute migration 0002 (v0.3): devices and execution evidence.
--
-- * A row in `gpus` is now a compute device: device_type 'gpu' or 'cpu'.
-- * `offered` records the provider's local opt-in on the machine itself (agent flags). The
--   scheduler never uses a device the agent has not offered, whatever the dashboard says.
-- * Workload capabilities move from the node to each device, so a machine can mix an old GPU
--   that cannot run a workload with a newer one that can.
-- * Jobs record where they executed and the evidence the server checked.

ALTER TABLE gpus ADD COLUMN device_type TEXT NOT NULL DEFAULT 'gpu';
ALTER TABLE gpus ADD COLUMN offered INTEGER NOT NULL DEFAULT 1;
ALTER TABLE gpus ADD COLUMN offer_note TEXT;
ALTER TABLE gpus ADD COLUMN display_active INTEGER;
ALTER TABLE gpus ADD COLUMN vram_free_mb INTEGER;
ALTER TABLE gpus ADD COLUMN cuda_driver_version INTEGER;
ALTER TABLE gpus ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]';

-- Simulated listings keep their workloads. Agent devices must be re-offered by an agent that
-- understands opt-in, so they stop receiving work until it reconnects.
UPDATE gpus SET capabilities = (SELECT n.capabilities FROM nodes n WHERE n.id = gpus.node_id)
 WHERE node_id IN (SELECT id FROM nodes WHERE kind = 'simulated');
UPDATE gpus SET offered = 0, offer_note = 'Reconnect with agent v0.3 or later to offer this device'
 WHERE node_id IN (SELECT id FROM nodes WHERE kind = 'agent');

ALTER TABLE jobs ADD COLUMN min_compute_capability TEXT;
ALTER TABLE jobs ADD COLUMN executed_on TEXT;   -- 'gpu' | 'cpu' | NULL (simulated or never ran)
ALTER TABLE jobs ADD COLUMN evidence TEXT;      -- JSON: what the server verified and how
