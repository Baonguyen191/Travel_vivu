CREATE TABLE places (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  name_en TEXT,
  category TEXT NOT NULL DEFAULT 'khac',
  location GEOGRAPHY(POINT, 4326),
  opening_hours JSONB,
  opening_hours_raw TEXT,
  ticket_price JSONB,
  dress_code TEXT,
  etiquette TEXT,
  avg_visit_minutes INT,
  indoor_ratio REAL,
  weather_sensitivity JSONB,
  best_time_of_day TEXT[],
  unsafe_conditions TEXT[],
  label_source TEXT NOT NULL DEFAULT 'default',
  website TEXT,
  source_url TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX places_location_gix ON places USING GIST (location);
CREATE INDEX places_category_idx ON places (category);

CREATE TABLE place_external_ids (
  place_id INT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
);
CREATE INDEX place_external_ids_place_idx ON place_external_ids (place_id);

CREATE TABLE raw_documents (
  id SERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  source_url TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  content_hash TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  http_status INT NOT NULL
);
CREATE INDEX raw_documents_url_idx ON raw_documents (source_url, fetched_at DESC);

CREATE TABLE place_chunks (
  id SERIAL PRIMARY KEY,
  place_id INT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_url TEXT,
  embedding VECTOR(1024),
  UNIQUE (place_id, content_hash)
);

CREATE TABLE place_images (
  id SERIAL PRIMARY KEY,
  place_id INT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  image_url TEXT NOT NULL UNIQUE,
  license TEXT,
  author TEXT,
  embedding VECTOR(768)
);

CREATE TABLE reviews (
  id SERIAL PRIMARY KEY,
  place_id INT REFERENCES places(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  rating REAL,
  aspects JSONB,
  source TEXT,
  created_at TIMESTAMPTZ
);

CREATE TABLE weather_cache (
  lat_grid REAL, lon_grid REAL, forecast_time TIMESTAMPTZ,
  temperature REAL, precip_prob REAL, precip_mm REAL,
  wind_speed REAL, uv_index REAL, weather_code INT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (lat_grid, lon_grid, forecast_time)
);

CREATE TABLE climate_normals (
  lat_grid REAL, lon_grid REAL, month INT,
  temp_avg REAL, precip_mm_avg REAL, rain_days REAL, wind_avg REAL,
  PRIMARY KEY (lat_grid, lon_grid, month)
);
