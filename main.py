import os
import json
import g4f
from github import Github, GithubException
from subprocess import check_output, CalledProcessError
from g4f.client import Client
from g4f.Provider import BaseProvider


def get_github_context():
    event_path = os.getenv('GITHUB_EVENT_PATH')
    if not event_path:
        raise EnvironmentError('GITHUB_EVENT_PATH not found in environment variables.')
    with open(event_path, 'r') as f:
        return json.load(f)


def main():
    try:
        # Inputs
        github_token = os.getenv('INPUT_GITHUB_TOKEN')
        temperature = float(os.getenv('INPUT_TEMPERATURE', '0.7'))
        provider_name = os.environ.get('INPUT_PROVIDER', 'auto')
        model_name = os.environ.get('INPUT_MODEL', 'o1-mini')
        custom_prompt = os.getenv('INPUT_PROMPT')

        event_name = os.getenv('GITHUB_EVENT_NAME')

        print(f"Temperature: {temperature}")
        print(f"Event name: {event_name}")
        if not github_token:
            raise ValueError('GitHub token not provided as input.')

        context = get_github_context()
        
        if event_name != 'pull_request':
            raise ValueError('This action only runs on pull_request events.')

        pr_number = context['pull_request']['number']
        base_ref = context['pull_request']['base']['ref']
        head_ref = context['pull_request']['head']['ref']

        g = Github(github_token)
        repo = g.get_repo(f"{context['repository']['owner']['login']}/{context['repository']['name']}")
        pull_request = repo.get_pull(pr_number)
        current_description = pull_request.body or ''

        if current_description.lower().__contains__('description done') and current_description.lower().__contains__('✅'):
            print("PR description is already marked as done. Skipping update.")
            return


        print(f"PR number: {pr_number}")
        print(f"Base ref: {base_ref}")
        print(f"Head ref: {head_ref}")
        
        # Set up Git
        os.system('git config --global --add safe.directory /github/workspace')
        os.system('git config --global user.name "github-actions[bot]"')
        os.system('git config --global user.email "github-actions[bot]@users.noreply.github.com"')

        # Fetch branches
        print(f"Fetching branches: {base_ref} and {head_ref}")
        fetch_result = os.system(f'git fetch origin {base_ref} {head_ref}')
        if fetch_result != 0:
            raise RuntimeError(f"Failed to fetch branches. Exit code: {fetch_result}")

        # Get the diff
        print(f"Getting diff between origin/{base_ref} and origin/{head_ref}")
        try:
            diff_output = check_output(f'git diff origin/{base_ref} origin/{head_ref}', shell=True, encoding='utf-8', stderr=-1)
        except CalledProcessError as e:
            print(f"Error output: {e.stderr}")
            raise RuntimeError(f"Failed to get diff. Exit code: {e.returncode}")

        print("Diff obtained successfully")
        print(f"Diff length: {len(diff_output)} characters")

        # Generate the PR description
        retry_count = 0
        max_retries = 10
        generated_description = None

        while retry_count < max_retries:
            generated_description = generate_description(diff_output, temperature, provider_name, model_name, custom_prompt)
            print(f"Generated description (attempt {retry_count + 1}):")
            print(generated_description[:100] + "..." if len(generated_description) > 100 else generated_description)
            if generated_description != "No message received" and len(generated_description.strip()) > 0:
                break
            retry_count += 1
            print(f"Retry {retry_count}/{max_retries}: No message received. Retrying...")

        if generated_description == "No message received":
            raise Exception("Failed to generate description after maximum retries")

        # Update the PR
        update_pr_description(github_token, context, pr_number, generated_description)

        print(f'Successfully updated PR #{pr_number} description.')

    except Exception as e:
        print(f'Action failed: {str(e)}')
        raise

def get_provider_class(provider_name):
    if provider_name == 'auto':
        return None
    try:
        provider_class = getattr(g4f.Provider, provider_name.split('.')[-1])
        if not issubclass(provider_class, BaseProvider):
            raise ValueError(f"Invalid provider: {provider_name}")
        return provider_class
    except AttributeError:
        raise ValueError(f"Provider not found: {provider_name}")
    
def generate_description(diff_output, temperature, provider_name, model_name, custom_prompt=None):
    
    provider_class = get_provider_class(provider_name)

    # Use custom prompt if provided, otherwise fallback to default
    if custom_prompt and custom_prompt.strip():
        prompt = f"{custom_prompt}\n\n**Diff:**\n{diff_output}"
    else:
        prompt = f"""
Please generate a **Pull Request description** for the provided diff, following these guidelines:
- The description should begin with a brief summary of the changes using at least 2 sentences and at most 6 sentences.
- Afterwards you should group changes using subheadings for related changes, e.g. Build process improvements, Replacing deprecated methods, etc., as level 3 markdown headings.
- Describe changes to each file with 1 or sentences in the following format: `- <file-name>: <description>`
- Do **not** include the words "Title" and "Description" in your output.
- Format your answer in **Markdown**.
- The description should reflect the changes made as best as possible. To do this, you should group related changes together

**Diff:**
{diff_output}"""

    client = Client(provider=provider_class)
    print(f"Sending request to GPT-4 with temperature {temperature}")
    chat_completion = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                'role': 'system',
                'content': 'You are a helpful assistant who generates pull request descriptions based on diffs.',
            },
            {
                'role': 'user',
                'content': prompt,
            },
        ],
        temperature=temperature,
        max_tokens=2048,
    )

    description = chat_completion.choices[0].message.content.strip()
    print(f"Received response from {model_name}. Length: {len(description)} characters")

    # Remove markdown code block from response if present (```markdown <content> ```)
    if description.startswith("```markdown"):
        description = description[11:]
        if description.endswith("```"):
            description = description[:-3]

    return description.strip()


def update_pr_description(github_token, context, pr_number, generated_description):
    g = Github(github_token)
    repo = g.get_repo(f"{context['repository']['owner']['login']}/{context['repository']['name']}")
    pull_request = repo.get_pull(pr_number)

    current_description = pull_request.body or ''
    auto_generated_marker = '> Automatically generated by [auto-pr-description](https://github.com/Marty-Byrde/auto-pr-description-g4f-action)'

    print("Evaluating base description that we want to keep...")
    # base_description ->  The description before the auto-generated marker that we want to keep
    if auto_generated_marker in current_description:
        base_description = current_description.split(auto_generated_marker)[0].strip()
    else:
        base_description = current_description.strip()

    print(f"Keeping this part of the description: {base_description}")

    new_description = f"""{base_description}\n\n{auto_generated_marker}\n\n{generated_description}"""

    try:
        if current_description and base_description not in new_description:
            print('Creating comment with original description...')
            pull_request.create_issue_comment(f'**Previous description**:\n\n{current_description}')
            print('Comment created successfully.')

        print('Updating PR description...')
        pull_request.edit(body=new_description)
        print('PR description updated successfully.')

    except GithubException as e:
        print(f'Error updating PR description: {e}')
        raise


if __name__ == '__main__':
    main()
