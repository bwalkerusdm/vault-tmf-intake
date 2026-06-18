openapi: 3.0.0
info:
  title: Get TMF Taxonomy
  version: 1.0.0
servers:
- url: https://vault-tmf-intake.vercel.app
security:
- bearerAuth: []
paths:
  /api/etmf/actions/get_taxonomy.py:
    post:
      operationId: getTaxonomy
      summary: Get the valid classification taxonomy to classify within
      description: 'Returns the flat list of valid type/subtype/classification triples (served from the
        indexed snapshot, no live Vault call). Classify ONLY within a triple that appears here — never
        invent one. The gate re-checks membership against this same source, so an off-list triple is always
        held.

        '
      requestBody:
        required: false
        content:
          application/json:
            schema:
              type: object
      responses:
        '200':
          description: The taxonomy
          content:
            application/json:
              schema:
                type: object
                properties:
                  taxonomy:
                    type: array
                    items:
                      type: object
                      properties:
                        type__v:
                          type: string
                        subtype__v:
                          type: string
                        classification__v:
                          type: string
                        label:
                          type: string
                        tmf_rm_v3:
                          type: string
                  count:
                    type: integer
                  source:
                    type: string
                    description: snapshot | vault_live
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      description: Use the GLEAN_BEARER_TOKEN value configured on the project.
