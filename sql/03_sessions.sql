-- SE-06 server-side sessions: the browser keeps a random token, the database keeps only its sha256 hash.
-- Expiry is decided here, so it survives restarts and cannot be extended by the client.
-- What the database enforces: idle expiry, logout and hash-only storage. Who may log in is the backend's job: vytal_app is
-- trusted to call start_session only after it has checked the password, so it can start a session for any user id.
SET search_path = vytal;

CREATE TABLE user_session (
    session_id BIGSERIAL NOT NULL,
    user_id INT NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (session_id),
    FOREIGN KEY (user_id) REFERENCES user_account (user_id) ON DELETE RESTRICT,
    CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CHECK (last_seen_at >= created_at),
    CHECK (ended_at IS NULL OR ended_at >= created_at)
);

-- SE-06: the idle timeouts, in one place
CREATE FUNCTION session_timeout(p_role text) RETURNS interval LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_role IN ('Staff', 'Admin') THEN interval '15 minutes'
                WHEN p_role = 'Donor' THEN interval '30 minutes' END
$$;

-- The session routines run as their owner, so the application role never reads or rewrites token hashes directly.
CREATE PROCEDURE start_session(p_user int, p_token_hash text, INOUT new_session_id bigint DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM user_account WHERE user_id = p_user AND is_active) THEN
        RAISE EXCEPTION 'account % is not active', p_user; END IF;
    INSERT INTO user_session (user_id, token_hash) VALUES (p_user, p_token_hash) RETURNING session_id INTO new_session_id;
END $$;

-- Returns the session's user and role and refreshes its idle window; returns NULLs (and ends the session) when it has
-- ended, timed out or its account was deactivated. It does not raise, so ending an expired session is committed.
CREATE PROCEDURE resume_session(p_token_hash text, INOUT p_user_id int DEFAULT NULL, INOUT p_role text DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
DECLARE s user_session%ROWTYPE; a user_account%ROWTYPE;
BEGIN
    p_user_id := NULL; p_role := NULL;
    SELECT * INTO s FROM user_session WHERE token_hash = p_token_hash AND ended_at IS NULL FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO a FROM user_account WHERE user_id = s.user_id;
    IF NOT a.is_active OR now() - s.last_seen_at > session_timeout(a.role) THEN
        UPDATE user_session SET ended_at = now() WHERE session_id = s.session_id;
        RETURN;
    END IF;
    UPDATE user_session SET last_seen_at = now() WHERE session_id = s.session_id;
    p_user_id := s.user_id; p_role := a.role;
END $$;

-- logout
CREATE PROCEDURE end_session(p_token_hash text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
BEGIN
    UPDATE user_session SET ended_at = now() WHERE token_hash = p_token_hash AND ended_at IS NULL;
END $$;
