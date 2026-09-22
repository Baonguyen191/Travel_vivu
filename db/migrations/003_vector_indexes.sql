CREATE INDEX place_chunks_embedding_idx ON place_chunks
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX place_images_embedding_idx ON place_images
  USING hnsw (embedding vector_cosine_ops);
