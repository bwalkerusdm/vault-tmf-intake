{
  "openapi": "3.0.0",
  "info": {
    "title": "Get TMF Taxonomy",
    "version": "1.0.0"
  },
  "servers": [
    {
      "url": "https://vault-tmf-intake.vercel.app"
    }
  ],
  "security": [
    {
      "bearerAuth": []
    }
  ],
  "paths": {
    "/api/etmf/actions/get_taxonomy.py": {
      "post": {
        "operationId": "getTaxonomy",
        "summary": "Return the valid TMF type/subtype/classification triples to classify within.",
        "requestBody": {
          "required": false,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {}
              }
            }
          }
        },
        "responses": {
          "200": {
            "description": "OK",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "properties": {
                    "taxonomy": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {}
                      }
                    },
                    "count": {
                      "type": "integer"
                    },
                    "source": {
                      "type": "string",
                      "description": "snapshot or vault_live"
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  },
  "components": {
    "securitySchemes": {
      "bearerAuth": {
        "type": "http",
        "scheme": "bearer"
      }
    }
  }
}
