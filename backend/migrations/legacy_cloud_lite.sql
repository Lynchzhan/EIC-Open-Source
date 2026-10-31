CREATE INDEX ix_agent_memories_agent_id ON agent_memories (agent_id);
CREATE INDEX ix_agent_memories_owner_id ON agent_memories (owner_id);
CREATE INDEX ix_agent_memories_tenant_id ON agent_memories (tenant_id);
CREATE INDEX ix_ai_apps_tenant_id ON ai_apps (tenant_id);
CREATE INDEX ix_ai_models_tenant_id ON ai_models (tenant_id);
CREATE INDEX ix_ai_prompts_tenant_id ON ai_prompts (tenant_id);
CREATE INDEX ix_chat_events_chat_id ON chat_events (chat_id);
CREATE INDEX ix_chat_events_request_id ON chat_events (request_id);
CREATE INDEX ix_chat_events_tenant_id ON chat_events (tenant_id);
CREATE INDEX ix_chat_messages_request_id ON chat_messages (request_id);
CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);
CREATE INDEX ix_chat_messages_tenant_id ON chat_messages (tenant_id);
CREATE INDEX ix_chat_sessions_owner_id ON chat_sessions (owner_id);
CREATE INDEX ix_chat_sessions_tenant_id ON chat_sessions (tenant_id);
CREATE INDEX ix_departments_parent_id ON departments (parent_id);
CREATE INDEX ix_departments_tenant_id ON departments (tenant_id);
CREATE INDEX ix_knowledge_bases_tenant_id ON knowledge_bases (tenant_id);
CREATE INDEX ix_knowledge_chunks_content_hash ON knowledge_chunks (content_hash);
CREATE INDEX ix_knowledge_chunks_document_id ON knowledge_chunks (document_id);
CREATE INDEX ix_knowledge_chunks_knowledge_base_id ON knowledge_chunks (knowledge_base_id);
CREATE INDEX ix_knowledge_chunks_parent_chunk_id ON knowledge_chunks (parent_chunk_id);
CREATE INDEX ix_knowledge_chunks_tenant_id ON knowledge_chunks (tenant_id);
CREATE INDEX ix_knowledge_documents_knowledge_base_id ON knowledge_documents (knowledge_base_id);
CREATE INDEX ix_knowledge_documents_tenant_id ON knowledge_documents (tenant_id);
CREATE INDEX ix_knowledge_eval_cases_eval_set_id ON knowledge_eval_cases (eval_set_id);
CREATE INDEX ix_knowledge_eval_cases_tenant_id ON knowledge_eval_cases (tenant_id);
CREATE INDEX ix_knowledge_eval_runs_eval_set_id ON knowledge_eval_runs (eval_set_id);
CREATE INDEX ix_knowledge_eval_runs_knowledge_base_id ON knowledge_eval_runs (knowledge_base_id);
CREATE INDEX ix_knowledge_eval_runs_tenant_id ON knowledge_eval_runs (tenant_id);
CREATE INDEX ix_knowledge_eval_sets_knowledge_base_id ON knowledge_eval_sets (knowledge_base_id);
CREATE INDEX ix_knowledge_eval_sets_tenant_id ON knowledge_eval_sets (tenant_id);
CREATE INDEX ix_knowledge_ingestion_runs_document_id ON knowledge_ingestion_runs (document_id);
CREATE INDEX ix_knowledge_ingestion_runs_tenant_id ON knowledge_ingestion_runs (tenant_id);
CREATE INDEX ix_legacy_records_source_table ON legacy_records (source_table);
CREATE INDEX ix_legacy_records_tenant_id ON legacy_records (tenant_id);
CREATE UNIQUE INDEX ix_permissions_code ON permissions (code);
CREATE INDEX ix_positions_tenant_id ON positions (tenant_id);
CREATE INDEX ix_resources_legacy_source_id ON resources (legacy_source_id);
CREATE INDEX ix_resources_owner_id ON resources (owner_id);
CREATE INDEX ix_resources_resource_type ON resources (resource_type);
CREATE INDEX ix_resources_tenant_id ON resources (tenant_id);
CREATE INDEX ix_roles_tenant_id ON roles (tenant_id);
CREATE INDEX ix_scheduled_tasks_owner_id ON scheduled_tasks (owner_id);
CREATE INDEX ix_scheduled_tasks_tenant_id ON scheduled_tasks (tenant_id);
CREATE INDEX ix_skills_tenant_id ON skills (tenant_id);
CREATE INDEX ix_task_runs_job_id ON task_runs (job_id);
CREATE INDEX ix_task_runs_task_id ON task_runs (task_id);
CREATE INDEX ix_task_runs_tenant_id ON task_runs (tenant_id);
CREATE UNIQUE INDEX ix_tenants_code ON tenants (code);
CREATE INDEX ix_tool_audits_chat_id ON tool_audits (chat_id);
CREATE INDEX ix_tool_audits_tenant_id ON tool_audits (tenant_id);
CREATE INDEX ix_tool_audits_user_id ON tool_audits (user_id);
CREATE INDEX ix_users_department_id ON users (department_id);
CREATE INDEX ix_users_position_id ON users (position_id);
CREATE INDEX ix_users_tenant_id ON users (tenant_id);
CREATE INDEX ix_users_username ON users (username);
CREATE TABLE agent_memories (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	owner_id BIGINT NOT NULL, 
	agent_id BIGINT, 
	content TEXT NOT NULL, 
	metadata JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id)
);
CREATE TABLE ai_apps (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	chat_model_id BIGINT, 
	prompt_id BIGINT, 
	knowledge_base_id BIGINT, 
	config JSON NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(chat_model_id) REFERENCES ai_models (id)
);
CREATE TABLE ai_models (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	provider VARCHAR(80) NOT NULL, 
	model_type VARCHAR(40) NOT NULL, 
	endpoint VARCHAR(500) NOT NULL, 
	model_name VARCHAR(200) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	embedding_dimensions INTEGER, 
	max_input_tokens INTEGER NOT NULL, 
	max_output_tokens INTEGER NOT NULL, 
	secret_encrypted TEXT, 
	secret_hint VARCHAR(24), 
	enabled BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE ai_prompts (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	content TEXT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE chat_events (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	chat_id BIGINT NOT NULL, 
	request_id VARCHAR(80) NOT NULL, 
	event_type VARCHAR(80) NOT NULL, 
	payload TEXT NOT NULL, 
	sequence INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE chat_messages (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	session_id BIGINT NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	request_id VARCHAR(80), 
	token_usage JSON NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(session_id) REFERENCES chat_sessions (id)
);
CREATE TABLE chat_sessions (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	owner_id BIGINT NOT NULL, 
	app_id BIGINT, 
	title VARCHAR(500) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id), 
	FOREIGN KEY(app_id) REFERENCES ai_apps (id)
);
CREATE TABLE departments (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	parent_id BIGINT, 
	name VARCHAR(200) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE knowledge_bases (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	embedding_model_id BIGINT, 
	rerank_model_id BIGINT, 
	chunk_size INTEGER NOT NULL, 
	chunk_overlap INTEGER NOT NULL, 
	status VARCHAR(40) NOT NULL, retrieval_strategy VARCHAR(32) DEFAULT 'full', retrieval_config JSON, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(embedding_model_id) REFERENCES ai_models (id), 
	FOREIGN KEY(rerank_model_id) REFERENCES ai_models (id)
);
CREATE TABLE knowledge_chunks (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	knowledge_base_id BIGINT NOT NULL, 
	document_id BIGINT NOT NULL, 
	parent_chunk_id BIGINT, 
	chunk_index INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	metadata JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_knowledge_chunks_document_index UNIQUE (document_id, chunk_index), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id), 
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id)
);
CREATE TABLE knowledge_documents (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	knowledge_base_id BIGINT NOT NULL, 
	file_id BIGINT, 
	source_name VARCHAR(500) NOT NULL, 
	content TEXT NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	error_message VARCHAR(1000) NOT NULL, created_at DATETIME, updated_at DATETIME, extraction_report JSON, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);
CREATE TABLE knowledge_eval_cases (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	eval_set_id BIGINT NOT NULL, 
	"query" TEXT NOT NULL, 
	expected_document_ids JSON NOT NULL, 
	expected_chunk_ids JSON NOT NULL, 
	expected_answer TEXT NOT NULL, 
	complexity VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(eval_set_id) REFERENCES knowledge_eval_sets (id)
);
CREATE TABLE knowledge_eval_runs (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	eval_set_id BIGINT NOT NULL, 
	knowledge_base_id BIGINT NOT NULL, 
	k INTEGER NOT NULL, 
	strategy VARCHAR(32) NOT NULL, 
	result JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(eval_set_id) REFERENCES knowledge_eval_sets (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);
CREATE TABLE knowledge_eval_sets (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	knowledge_base_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	last_result JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);
CREATE TABLE knowledge_ingestion_runs (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	document_id BIGINT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	chunk_count INTEGER NOT NULL, 
	embedding_count INTEGER NOT NULL, 
	error_message VARCHAR(1000) NOT NULL, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id)
);
CREATE TABLE legacy_records (
	id BIGINT NOT NULL, 
	source_table VARCHAR(160) NOT NULL, 
	source_id VARCHAR(160) NOT NULL, 
	tenant_id BIGINT, 
	payload JSON NOT NULL, 
	imported_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_legacy_records_source UNIQUE (source_table, source_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE permissions (
	id VARCHAR(160) NOT NULL, 
	code VARCHAR(160) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	category VARCHAR(80) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE positions (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE resources (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	resource_type VARCHAR(80) NOT NULL, 
	legacy_source_id BIGINT, 
	name VARCHAR(500) NOT NULL, 
	owner_id BIGINT, 
	data JSON NOT NULL, 
	is_deleted BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id)
);
CREATE TABLE role_permissions (
	role_id BIGINT NOT NULL, 
	permission_id VARCHAR(160) NOT NULL, 
	PRIMARY KEY (role_id, permission_id), 
	FOREIGN KEY(role_id) REFERENCES roles (id), 
	FOREIGN KEY(permission_id) REFERENCES permissions (id)
);
CREATE TABLE roles (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE scheduled_tasks (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	cron VARCHAR(120) NOT NULL, 
	payload JSON NOT NULL, 
	task_type VARCHAR(24) NOT NULL, 
	run_at DATETIME, 
	max_retries INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	last_run_at DATETIME, 
	owner_id BIGINT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE skills (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	skill_type VARCHAR(40) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	config JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE TABLE task_runs (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	task_id BIGINT NOT NULL, 
	job_id VARCHAR(80), 
	status VARCHAR(32) NOT NULL, 
	attempt INTEGER NOT NULL, 
	result JSON NOT NULL, 
	error_message VARCHAR(1000) NOT NULL, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(task_id) REFERENCES scheduled_tasks (id)
);
CREATE TABLE tenants (
	id BIGINT NOT NULL, 
	code INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE tool_audits (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	user_id BIGINT, 
	chat_id BIGINT, 
	tool_name VARCHAR(200) NOT NULL, 
	tool_type VARCHAR(40) NOT NULL, 
	outcome VARCHAR(32) NOT NULL, 
	input_digest VARCHAR(64) NOT NULL, 
	output_digest VARCHAR(64), 
	duration_ms INTEGER NOT NULL, 
	error_message VARCHAR(1000) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE TABLE user_roles (
	user_id BIGINT NOT NULL, 
	role_id BIGINT NOT NULL, 
	PRIMARY KEY (user_id, role_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(role_id) REFERENCES roles (id)
);
CREATE TABLE users (
	id BIGINT NOT NULL, 
	tenant_id BIGINT NOT NULL, 
	username VARCHAR(120) NOT NULL, 
	nickname VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	is_super_admin BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	phone VARCHAR(32) NOT NULL, 
	email VARCHAR(160) NOT NULL, 
	department_id BIGINT, 
	position_id BIGINT, 
	avatar_url TEXT, 
	approval_status VARCHAR(20) NOT NULL, 
	profile JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_users_tenant_username UNIQUE (tenant_id, username), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
