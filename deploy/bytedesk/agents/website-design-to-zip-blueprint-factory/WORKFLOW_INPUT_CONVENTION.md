# Website Blueprint Workflow Input Convention

Use this convention when asking `website-design-to-zip-blueprint-factory` to
create and deliver a website zip.

Required:
- `client_name`: client or business name.
- `website_goal`: what the website should accomplish.
- `client_drive_folder_id`: root client Drive folder id, unless
  `client_website_folder_id` is supplied.

Optional:
- `client_website_folder_id`: explicit website-related Drive folder id. This
  wins over inference.
- `pages`: requested pages or sections.
- `style_direction`: visual style request.
- `brand_constraints`: colors, logo usage, typography, voice, or compliance
  requirements.
- `required_assets`: assets that must be used.
- `reference_sites`: examples to consider.
- `google_subject`: delegated Google Workspace subject.

Drive destination rule:
- If `client_website_folder_id` is present, upload the zip there.
- Otherwise use `client_drive_folder_id` as `parent_folder_id` and
  `folder_name: "Website"` so the Google Drive connector can find or create the
  website folder.
