from celery import Celery
import time
import os 
import json
import subprocess
from celery import states
import pytz
import datetime
from github_client import *
from common import *
from preprint import *
from github import Github, UnknownObjectException
from dotenv import load_dotenv
import logging
import requests
from flask import Response
import shutil
import base64

DOI_PREFIX = "10.55458"
DOI_SUFFIX = "neurolibre"
JOURNAL_NAME = "NeuroLibre"
PAPERS_PATH = "https://neurolibre.org/papers"
PRODUCTION_BINDERHUB = "https://binder-mcgill.conp.cloud"
PREVIEW_SERVER = "https://preview.neurolibre.org"

"""
Configuration START
"""
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# IMPORTANT, secrets will not be loaded otherwise.
load_dotenv()

# Setting Redis as both backend and broker
celery_app = Celery('neurolibre_celery_tasks', backend='redis://localhost:6379/1', broker='redis://localhost:6379/0')

celery_app.conf.update(task_track_started=True)

"""
Configuration END
"""

# Set timezone US/Eastern (Montreal)
def get_time():
    """
    To be printed on issue comment updates for 
    background tasks.
    """
    tz = pytz.timezone('US/Eastern')
    now = datetime.datetime.now(tz)
    cur_time = now.strftime('%Y-%m-%d %H:%M:%S %Z')
    return cur_time

@celery_app.task(bind=True)
def sleep_task(self, seconds):
    """
    To test async task functionality
    """
    for i in range(seconds):
        time.sleep(1)
        self.update_state(state='PROGRESS', meta={'remaining': seconds - i - 1})
    return 'done sleeping for {} seconds'.format(seconds)

@celery_app.task(bind=True)
def rsync_data_task(self, comment_id, issue_id, project_name, reviewRepository):
    """
    Uploading data to the production server 
    from the test server.
    """
    task_title = "DATA TRANSFER (Preview --> Preprint)"
    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    remote_path = os.path.join("neurolibre-preview:", "DATA", project_name)
    try:
        # TODO: improve this, subpar logging.
        f = open("/DATA/data_synclog.txt", "a")
        f.write(remote_path)
        f.close()
        now = get_time()
        self.update_state(state=states.STARTED, meta={'message': f"Transfer started {now}"})
        gh_template_respond(github_client,"started",task_title,reviewRepository,issue_id,task_id,comment_id, "")
        process = subprocess.Popen(["/usr/bin/rsync", "-avR", remote_path, "/"], stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        output = process.communicate()[0]
        ret = process.wait()
        #logging.info(output)
    except subprocess.CalledProcessError as e:
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"{e.output}")
        self.update_state(state=states.FAILURE, meta={'message': e.output})
    # Performing a final check
    if os.path.exists(os.path.join("/DATA", project_name)):
        if len(os.listdir(os.path.join("/DATA", project_name))) == 0:
            # Directory exists but empty
            self.update_state(state=states.FAILURE, meta={'message': f"Directory exists but empty {project_name}"})
            gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Directory exists but empty: {project_name}")
        else:
            # Directory exists and not empty
            gh_template_respond(github_client,"success",task_title,reviewRepository,issue_id,task_id,comment_id, output)
            self.update_state(state=states.SUCCESS, meta={'message': f"Data sync has been completed for {project_name}"})
    else:
        # Directory does not exist
        self.update_state(state=states.FAILURE, meta={'message': f"Directory does not exist {project_name}"})
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Directory does not exist: {project_name}")

@celery_app.task(bind=True)
def rsync_book_task(self, repo_url, commit_hash, comment_id, issue_id, reviewRepository, server):
    """
    Moving the book from the test to the production
    server. This book is expected to be built from
    a roboneurolibre repository. 

    Once the book is available on the production server,
    content is symlinked to a DOI formatted directory (Nginx configured) 
    to enable DOI formatted links.
    """
    task_title = "REPRODUCIBLE PREPRINT TRANSFER (Preview --> Preprint)"
    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    [owner,repo,provider] = get_owner_repo_provider(repo_url,provider_full_name=True)
    if owner != "roboneurolibre": 
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Repository is not under roboneurolibre organization!")
        self.request.revoke(terminate=True)
        return
    commit_hash = format_commit_hash(repo_url,commit_hash)
    logging.info(f"{owner}{provider}{repo}{commit_hash}")
    remote_path = os.path.join("neurolibre-preview:", "DATA", "book-artifacts", owner, provider, repo, commit_hash + "*")
    try:
        # TODO: improve this, subpar logging.
        f = open("/DATA/synclog.txt", "a")
        f.write(remote_path)
        f.close()
        now = get_time()
        self.update_state(state=states.STARTED, meta={'message': f"Transfer started {now}"})
        gh_template_respond(github_client,"started",task_title,reviewRepository,issue_id,task_id,comment_id, "")
        #logging.info("Calling subprocess")
        process = subprocess.Popen(["/usr/bin/rsync", "-avR", remote_path, "/"], stdout=subprocess.PIPE,stderr=subprocess.STDOUT) 
        output = process.communicate()[0]
        ret = process.wait()
        logging.info(output)
    except subprocess.CalledProcessError as e:
        #logging.info("Subprocess exception")
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"{e.output}")
        self.update_state(state=states.FAILURE, meta={'message': e.output})
    # Check if GET works for the complicated address
    results = book_get_by_params(commit_hash=commit_hash)
    if not results:
        self.update_state(state=states.FAILURE, meta={'message': f"Cannot retreive book at {commit_hash}"})
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Cannot retreive book at {commit_hash}")
    else:
        # Symlink production book to attain a proper URL
        book_path = os.path.join("/DATA", "book-artifacts", owner, provider, repo, commit_hash , "_build" , "html")
        iid = "{:05d}".format(issue_id)
        doi_path =  os.path.join("/DATA","10.55458",f"neurolibre.{iid}")
        process_mkd = subprocess.Popen(["mkdir", doi_path], stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        output_mkd = process_mkd.communicate()[0]
        ret_mkd = process_mkd.wait()

        for item in os.listdir(book_path):
            source_path = os.path.join(book_path, item)
            target_path = os.path.join(doi_path, item)
            if os.path.isdir(source_path):
                os.symlink(source_path, target_path, target_is_directory=True)
            else:
                os.symlink(source_path, target_path)
        # Check if symlink successful
        if os.path.exists(os.path.join(doi_path)):
            message = f"<a href=\"{server}/10.55458/neurolibre.{iid}\">Reproducible Preprint URL (DOI formatted)</a><p><a href=\"{server}/{book_path}\">Reproducible Preprint (bare URL)</a></p>"
            gh_template_respond(github_client,"success",task_title,reviewRepository,issue_id,task_id,comment_id, message)
            self.update_state(state=states.SUCCESS, meta={'message': message})
        else:
            self.update_state(state=states.FAILURE, meta={'message': f"Cannot sync book at {commit_hash}"})
            gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, output)

@celery_app.task(bind=True)
def fork_configure_repository_task(self, source_url, comment_id, issue_id, reviewRepository):
    task_title = "INITIATE PRODUCTION (Fork and Configure)"
    
    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    
    now = get_time()
    self.update_state(state=states.STARTED, meta={'message': f"Transfer started {now}"})
    gh_template_respond(github_client,"started",task_title,reviewRepository,issue_id,task_id,comment_id, "")
    
    forked_name = gh_forkify_name(source_url)
    # First check if a fork already exists.
    fork_exists  = False
    try: 
        github_client.get_repo(forked_name)
        fork_exists = True
    except UnknownObjectException as e:
        gh_template_respond(github_client,"started",task_title,reviewRepository,issue_id,task_id,comment_id, "Started forking into roboneurolibre.")
        logging.info(e.data['message'] + "--> Forking")

    if not fork_exists:
        try:
            forked_repo = gh_fork_repository(github_client,source_url)
        except Exception as e:
            gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Cannot fork the repository into {GH_ORGANIZATION}! \n {str(e)}")
            self.request.revoke(terminate=True)
            return

        forked_repo = None
        retry_count = 0
        max_retries = 5

        while retry_count < max_retries and not forked_repo:
            time.sleep(15)
            retry_count += 1
            try:
                forked_repo = github_client.get_repo(forked_name)
            except Exception:
                pass

        if not forked_repo and retry_count == max_retries:
            gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Forked repository is still not available after {max_retries*15} seconds! Please check if the repository is available under roboneurolibre organization, then try again.")
            self.request.revoke(terminate=True)
            return
    else:
        logging.info(f"Fork already exists {source_url}, moving on with configurations.")
    
    gh_template_respond(github_client,"started",task_title,reviewRepository,issue_id,task_id,comment_id, "Forked repo has become available. Proceeding with configuration updates.")

    jb_config = gh_get_jb_config(github_client,forked_name)
    jb_toc = gh_get_jb_toc(github_client,forked_name)

    if not jb_config or not jb_toc:
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Could not load _config.yml or _toc.yml under the content directory of {forked_name}")
        self.request.revoke(terminate=True)
        return

    if not jb_config['launch_buttons']:
        jb_config['launch_buttons'] = {}

    # Configure the book to use the production BinderHUB
    jb_config['launch_buttons']['binderhub_url'] = PRODUCTION_BINDERHUB

    # Update repository address
    if not jb_config['repository']:
        jb_config['repository'] = {}
    jb_config['repository']['url'] = f"https://github.com/{forked_name}"

    # Update configuration file in the forked repo
    response = gh_update_jb_config(github_client,forked_name,jb_config)

    if not response['status']:
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Could not update _config.yml for {forked_name}: \n {response['message']}")
        self.request.revoke(terminate=True)
        return

    if 'parts' in jb_toc:
        jb_toc['parts'].append({
            "caption": JOURNAL_NAME,
            "chapters": [{
                "url": f"{PAPERS_PATH}/{DOI_PREFIX}/{DOI_SUFFIX}.{issue_id:05d}",
                "title": "Citable PDF and archives"
            }]
        })
    
    if 'chapters' in jb_toc:
        jb_toc['chapters'].append({
            "url": f"{PAPERS_PATH}/{DOI_PREFIX}/{DOI_SUFFIX}.{issue_id:05d}",
            "title": "Citable PDF and archives"
        })

    if jb_toc['format'] == 'jb-article' and 'sections' in jb_toc:
        jb_toc['sections'].append({
            "url": f"{PAPERS_PATH}/{DOI_PREFIX}/{DOI_SUFFIX}.{issue_id:05d}",
            "title": "Citable PDF and archives"
        })
    
    # Update TOC file in the forked repo
    response = gh_update_jb_toc(github_client,forked_name,jb_toc)

    if not response['status']:
        gh_template_respond(github_client,"failure",task_title,reviewRepository,issue_id,task_id,comment_id, f"Could not update toc.yml for {forked_name}: \n {response['message']}")
        self.request.revoke(terminate=True)
        return
    
    gh_template_respond(github_client,"success",task_title,reviewRepository,issue_id,task_id,comment_id, f"Please confirm that the <a href=\"https://github.com/{forked_name}\">forked repository</a> is available and (<code>_toc.yml</code> and <code>_config.ymlk</code>) properly configured.")

def binder_stream(response, github_client,lock_filename, task_id, payload):
    start_time = time.time()
    messages = []
    n_updates = 0
    for line in response.iter_lines():
        if line:
            event_string = line.decode("utf-8")
            try:
                event = json.loads(event_string.split(': ', 1)[1])
                # https://binderhub.readthedocs.io/en/latest/api.html
                if event.get('phase') == 'failed':
                    message = event.get('message')
                    response.close()
                    messages.append(message)
                    gh_template_respond(github_client,"failure","Binder build has failed &#129344;",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                    # Remove the lock as binder build failed.
                    #app.logger.info(f"[FAILED] BinderHub build {binderhub_request}.")
                    os.remove(lock_filename)
                    return
                message = event.get('message')
                if message:
                    yield message
                    messages.append(message)
                    elapsed_time = time.time() - start_time
                    # Update issue every two minutes
                    if elapsed_time >= 120:
                        n_updates = n_updates + 1
                        gh_template_respond(github_client,"started",payload['task_title'] + f" {n_updates*2} minutes passed",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                        start_time = time.time()
            except GeneratorExit:
                pass
            except:
                pass

"""
TODO IMPORTANT 

EITHER CALL GENERATOR MULTIPLE TIMES, OR MOVE IT TO THE 
BODY OF THE TASK, OTHERWISE UPDATES ARE NOT RECEIVED. 
"""
@celery_app.task(bind=True)
def preview_build_book_task(self, payload):

    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    binderhub_request = run_binder_build_preflight_checks(payload['repo_url'],
                                                          payload['commit_hash'],
                                                          payload['rate_limit'],
                                                          payload['binder_name'],
                                                          payload['domain_name'])
    lock_filename = get_lock_filename(payload['repo_url'])
    response = requests.get(binderhub_request, stream=True)
    gh_template_respond(github_client,"started",payload['task_title'],payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Running for: {binderhub_request}")
    if response.ok:
        # Create binder_stream generator object
        def generate():
            #start_time = time.time()
            #messages = []
            #n_updates = 0
            for line in response.iter_lines():
                if line:
                    event_string = line.decode("utf-8")
                    try:
                        event = json.loads(event_string.split(': ', 1)[1])
                        # https://binderhub.readthedocs.io/en/latest/api.html
                        if event.get('phase') == 'failed':
                            message = event.get('message')
                            yield message
                            response.close()
                            #messages.append(message)
                            #gh_template_respond(github_client,"failure","Binder build has failed &#129344;",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                            # Remove the lock as binder build failed.
                            #app.logger.info(f"[FAILED] BinderHub build {binderhub_request}.")
                            os.remove(lock_filename)
                            return
                        message = event.get('message')
                        if message:
                            yield message
                            #messages.append(message)
                            #elapsed_time = time.time() - start_time
                            # Update issue every two minutes
                            #if elapsed_time >= 120:
                            #    n_updates = n_updates + 1
                            #    gh_template_respond(github_client,"started",payload['task_title'] + f" {n_updates*2} minutes passed",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                            #    start_time = time.time()
                    except GeneratorExit:
                        pass
                    except:
                        pass
        # Use the generator object as the source of flask eventstream response
        binder_response = Response(generate(), mimetype='text/event-stream')
        # Fetch all the yielded messages
    binder_logs = binder_response.get_data(as_text=True)
    binder_logs = "".join(binder_logs)
    # After the upstream closes, check the server if there's 
    # a book built successfully.
    book_status = book_get_by_params(commit_hash=payload['commit_hash'])
    # For now, remove the block either way.
    # The main purpose is to avoid triggering
    # a build for the same request. Later on
    # you may choose to add dead time after a successful build.
    os.remove(lock_filename)
        # Append book-related response downstream
    if not book_status:
        # These flags will determine how the response will be 
        # interpreted and returned outside the generator
        gh_template_respond(github_client,"failure","Binder build has failed &#129344;",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], "The next comment will forward the logs")
        issue_comment = []
        msg = f"<p>&#129344; We ran into a problem building your book. Please see the log files below.</p><details><summary> <b>BinderHub build log</b> </summary><pre><code>{binder_logs}</code></pre></details><p>If the BinderHub build looks OK, please see the Jupyter Book build log(s) below.</p>"
        issue_comment.append(msg)
        owner,repo,provider = get_owner_repo_provider(payload['repo_url'],provider_full_name=True)
        # Retreive book build and execution report logs.
        book_logs = book_log_collector(owner,repo,provider,payload['commit_hash'])
        issue_comment.append(book_logs)
        msg = "<p>&#128030; After inspecting the logs above, you can interactively debug your notebooks on our <a href=\"https://binder.conp.cloud\">BinderHub server</a>.</p> <p>For guidelines, please see <a href=\"https://docs.neurolibre.org/en/latest/TEST_SUBMISSION.html#debugging-for-long-neurolibre-submission\">the relevant documentation.</a></p>"
        issue_comment.append(msg)
        issue_comment = "\n".join(issue_comment)
        # Send a new comment
        gh_create_comment(github_client, payload['review_repository'],payload['issue_id'],issue_comment)
    else:
        gh_template_respond(github_client,"success","Successfully built", payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"The next comment will forward the logs")
        issue_comment = []
        gh_create_comment(github_client, payload['review_repository'],payload['issue_id'],book_status[0]['book_url'])

@celery_app.task(bind=True)
def zenodo_create_buckets_task(self, payload):
    
    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id

    gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'])

    fname = f"zenodo_deposit_NeuroLibre_{payload['issue_id']:05d}.json"
    local_file = os.path.join(get_deposit_dir(payload['issue_id']), fname)

    if os.path.exists(local_file):
        msg = f"Zenodo records already exist for this submission on NeuroLibre servers: {fname}. Please proceed with data uploads if the records are valid. Flush the existing records otherwise."
        gh_template_respond(github_client,"exists",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'],msg)
        self.request.revoke(terminate=True)
        return
    
    data = payload['paper_data']

    # We need to go through some affiliation mapping here.
    affiliation_mapping = {str(affiliation['index']): affiliation['name'] for affiliation in data['affiliations']}
    first_affiliations = []
    for author in data['authors']:
        if isinstance(author['affiliation'],int):
            affiliation_index = author['affiliation']
        else:
            affiliation_indices = [affiliation_index for affiliation_index in author['affiliation'].split(',')]
            affiliation_index = affiliation_indices[0]
        first_affiliation = affiliation_mapping[str(affiliation_index)]
        first_affiliations.append(first_affiliation)

    for ii in range(len(data['authors'])):
        data['authors'][ii]['affiliation'] = first_affiliations[ii]
    
    # To deal with some typos, also with orchid :) 
    valid_field_names = {'name', 'orcid', 'affiliation'}
    for author in data['authors']:
        invalid_fields = []
        for field in author:
            if field not in valid_field_names:
                invalid_fields.append(field)
        
        for invalid_field in invalid_fields:
            valid_field = None
            for valid_name in valid_field_names:
                if valid_name.lower() in invalid_field.lower() or (valid_name == 'orcid' and invalid_field.lower() == 'orchid'):
                    valid_field = valid_name
                    break
            
            if valid_field:
                author[valid_field] = author.pop(invalid_field)

        if author.get('orcid') is None:
            author.pop('orcid')

    collect = {}
    for archive_type in payload['archive_assets']:
                gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Creating Zenodo buckets for {archive_type}")
                r = zenodo_create_bucket(data['title'],
                                         archive_type,
                                         data['authors'],
                                         payload['repository_url'],
                                         payload['issue_id'])
                collect[archive_type] = r
                # Rate limit
                time.sleep(2)
    
    if {k: v for k, v in collect.items() if 'reason' in v}:
        # This means at least one of the deposits has failed.
        logging.info(f"Caught an issue with the deposit. A record (JSON) will not be created.")

        # Delete deposition if succeeded for a certain resource
        remove_dict = {k: v for k, v in collect.items() if not 'reason' in v }
        for key in remove_dict:
            logging.info("Deleting " + remove_dict[key]["links"]["self"])
            tmp = zenodo_delete_bucket(remove_dict[key]["links"]["self"])
            time.sleep(1)
            # Returns 204 if successful, cast str to display
            collect[key + "_deleted"] = str(tmp)
        gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"{collect}")
    else:
        # This means that all requested deposits are successful
        print(f'Writing {local_file}...')
        with open(local_file, 'w') as outfile:
            json.dump(collect, outfile)
        gh_template_respond(github_client,"success",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Zenodo records have been created successfully: \n {collect}")

@celery_app.task(bind=True)
def zenodo_upload_book_task(self, payload):

    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    
    gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'])

    owner,repo,provider = get_owner_repo_provider(payload['repository_url'],provider_full_name=True)
    
    fork_url = f"https://{provider}/roboneurolibre/{repo}"
    commit_fork = format_commit_hash(fork_url,"HEAD")

    local_path = os.path.join("/DATA", "book-artifacts", "roboneurolibre", provider, repo, commit_fork, "_build", "html")
    # Descriptive file name
    zenodo_file = os.path.join(get_archive_dir(payload['issue_id']),f"JupyterBook_10.55458_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}")
    # Zip it!
    shutil.make_archive(zenodo_file, 'zip', local_path)
    zpath = zenodo_file + ".zip"

    response = zenodo_upload_book(zpath,payload['bucket_url'],payload['issue_id'],commit_fork)

    if not response:
        gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Cannot upload {zpath} to {payload['bucket_url']}")
    else:
        tmp = f"zenodo_uploaded_book_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}.json"
        log_file = os.path.join(get_deposit_dir(payload['issue_id']), tmp)
        with open(log_file, 'w') as outfile:
                json.dump(response.json(), outfile)
        gh_template_respond(github_client,"success",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Successful {zpath} to {payload['bucket_url']}")
    
@celery_app.task(bind=True)
def zenodo_upload_repository_task(self, payload):

    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    
    gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'])

    owner,repo,provider = get_owner_repo_provider(payload['repository_url'],provider_full_name=True)
    
    fork_url = f"https://{provider}/roboneurolibre/{repo}"
    commit_fork = format_commit_hash(fork_url,"HEAD")
    
    default_branch = get_default_branch(github_client,fork_url)

    download_url = f"{fork_url}/archive/refs/heads/{default_branch}.zip"

    zenodo_file = os.path.join(get_archive_dir(payload['issue_id']),f"GitHubRepo_10.55458_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}.zip")

    # REFACTOR HERE AND MANAGE CONDITIONS CLEANER.
    # Try main first
    resp = os.system(f"wget -O {zenodo_file} {download_url}")
    if resp != 0:
        gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Cannot download: {download_url}")
        self.request.revoke(terminate=True)
        return
    else:
        response = zenodo_upload_repository(zenodo_file,payload['bucket_url'],payload['issue_id'],commit_fork) 
        if not response:
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Cannot upload {zenodo_file} to {payload['bucket_url']}")
        else:
            tmp = f"zenodo_uploaded_repository_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}.json"
            log_file = os.path.join(get_deposit_dir(payload['issue_id']), tmp)
            with open(log_file, 'w') as outfile:
                    json.dump(response.json(), outfile)
            gh_template_respond(github_client,"success",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Successful {zenodo_file} to {payload['bucket_url']}")

@celery_app.task(bind=True)
def zenodo_upload_docker_task(self, payload):

    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    
    gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'])

    owner,repo,provider = get_owner_repo_provider(payload['repository_url'],provider_full_name=True)
    
    fork_url = f"https://{provider}/roboneurolibre/{repo}"
    commit_fork = format_commit_hash(fork_url,"HEAD")

    record_name = item_to_record_name("docker")

    tar_file = os.path.join(get_archive_dir(payload['issue_id']),f"{record_name}_10.55458_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}.tar.gz")
    check_docker = os.path.exists(tar_file)

    if check_docker:
        msg = "Docker image already exists, uploading to zenodo."
        gh_template_respond(github_client,"started",payload['task_title'] + " `uploading (3/3)`", payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'],msg)
        # If image exists but could not upload due to a previous issue.
        response = zenodo_upload_item(tar_file,payload['bucket_url'],payload['issue_id'],commit_fork,"docker")
        if not response:
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Cannot upload {tar_file} to {payload['bucket_url']}")
        else:
            tmp = f"zenodo_uploaded_docker_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}.json"
            log_file = os.path.join(get_deposit_dir(payload['issue_id']), tmp)
            with open(log_file, 'w') as outfile:
                    json.dump(response.json(), outfile)
            gh_template_respond(github_client,"success",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Successful {tar_file} to {payload['bucket_url']}")
    else:
        # Get the lookup_table.tsv entry (from the preview server) for the fork_url
        lut = get_resource_lookup(PREVIEW_SERVER,True,fork_url)

        if not lut:
            # Terminate ERROR
            msg = f"Looks like there's not a successful book build record for {fork_url}"
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg)
            self.request.revoke(terminate=True)
            return

        msg = f"Found docker image: \n {lut}"
        gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'],msg)

        # Login to the private registry to pull images
        r = docker_login()
        
        if not r['status']:
            msg = f"Cannot login to NeuroLibre private docker registry. \n {r['message']}"
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg)
            self.request.revoke(terminate=True)
            return

        msg = f"Pulling docker image: \n {lut['docker_image']}"
        gh_template_respond(github_client,"started",payload['task_title'] + " `pulling (1/3)`", payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'],msg)

        # The lookup table (lut) should contain a docker image (see get_resource_lookup)
        r = docker_pull(lut['docker_image'])
        if not r['status']:
            msg = f"Cannot pull the docker image \n {r['message']}"
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg)
            self.request.revoke(terminate=True)
            return
        
        msg = f"Exporting docker image: \n {lut['docker_image']}"
        gh_template_respond(github_client,"started",payload['task_title'] + " `exporting (2/3)`", payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'],msg)

        r = docker_save(lut['docker_image'],payload['issue_id'],commit_fork)
        if not r[0]['status']:
            msg = f"Cannot save the docker image \n {r[0]['message']}"
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg)
            self.request.revoke(terminate=True)
            return

        tar_file = r[1]

        msg = f"Uploading docker image: \n {tar_file}"
        gh_template_respond(github_client,"started",payload['task_title'] + " `uploading (3/3)`", payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'],msg)

        response = zenodo_upload_item(tar_file,payload['bucket_url'],payload['issue_id'],commit_fork,"docker")
        
        if not response:
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Cannot upload {tar_file} to {payload['bucket_url']}")
        else:
            tmp = f"zenodo_uploaded_docker_NeuroLibre_{payload['issue_id']:05d}_{commit_fork[0:6]}.json"
            log_file = os.path.join(get_deposit_dir(payload['issue_id']), tmp)
            with open(log_file, 'w') as outfile:
                    json.dump(response.json(), outfile)
            gh_template_respond(github_client,"success",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Successful {tar_file} to {payload['bucket_url']}")

        r = docker_logout()
        # No need to break the operation this fails, just log.
        if not r['status']:
            logging.info("Problem with docker logout.")

@celery_app.task(bind=True)
def zenodo_publish_task(self, payload):
    
    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)
    task_id = self.request.id
    
    gh_template_respond(github_client,"started",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'])

    response = zenodo_publish(payload['issue_id'])
    if response == "no-record-found":
        msg = "<br> :neutral_face: I could not find any Zenodo-related records on NeuroLibre servers. Maybe start with <code>roboneuro zenodo create buckets</code>?"
        gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg)
        return
    else:
        # Confirm that all items are published.
        # TODO: Check this 
        publish_status = zenodo_confirm_status(payload['issue_id'],"published")
        # If all items are published, success. Add DOIs.
        if publish_status[0]:
            response.append("\n I will issue commands to set DOIs for the reproducibility assets. I'll talk to myself a bit, but don't worry, I am not an evil AGI (yet :smiling_imp:).")
            msg = "\n".join(response)
            gh_template_respond(github_client,"success",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg, False)
            # Set DOIs. This part is a little crazy, because roboneuro will be 
            # telling itself what to do. Like father, like son.
            dois = zenodo_collect_dois(payload['issue_id'])
            for key in dois.keys():
                command = f"@roboneuro set {dois[key]} as {key} archive"
                gh_create_comment(github_client,payload['review_repository'],payload['issue_id'],command)
                time.sleep(1)
        else:
            # Some one None 
            response.append(f"\n Looks like there's a problem. {publish_status[1]} reproducibility assets are archived.")
            msg = "\n".join(response)
            gh_template_respond(github_client,"failure",payload['task_title'], payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], msg, False)


### DUPLICATION FOR NOW, SAVING THE DAY.

@celery_app.task(bind=True)
def preview_build_book_test_task(self, payload):

    task_id = self.request.id
    owner,repo,provider = get_owner_repo_provider(payload['repo_url'],provider_full_name=True)
    binderhub_request = run_binder_build_preflight_checks(payload['repo_url'],
                                                          payload['commit_hash'],
                                                          payload['rate_limit'],
                                                          payload['binder_name'],
                                                          payload['domain_name'])
    lock_filename = get_lock_filename(payload['repo_url'])
    response = requests.get(binderhub_request, stream=True)
    mail_body = f"Runtime environment build has been started <code>{task_id}</code> If successful, it will be followed by the Jupyter Book build."
    send_email_celery(payload['email'],payload['mail_subject'],mail_body)
    #gh_template_respond(github_client,"started",payload['task_title'],payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"Running for: {binderhub_request}")
    if response.ok:
        # Create binder_stream generator object
        def generate():
            #start_time = time.time()
            #messages = []
            #n_updates = 0
            for line in response.iter_lines():
                if line:
                    event_string = line.decode("utf-8")
                    try:
                        event = json.loads(event_string.split(': ', 1)[1])
                        # https://binderhub.readthedocs.io/en/latest/api.html
                        if event.get('phase') == 'failed':
                            message = event.get('message')
                            yield message
                            response.close()
                            #messages.append(message)
                            #gh_template_respond(github_client,"failure","Binder build has failed &#129344;",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                            # Remove the lock as binder build failed.
                            #app.logger.info(f"[FAILED] BinderHub build {binderhub_request}.")
                            os.remove(lock_filename)
                            return
                        message = event.get('message')
                        if message:
                            yield message
                            #messages.append(message)
                            #elapsed_time = time.time() - start_time
                            # Update issue every two minutes
                            #if elapsed_time >= 120:
                            #    n_updates = n_updates + 1
                            #    gh_template_respond(github_client,"started",payload['task_title'] + f" {n_updates*2} minutes passed",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                            #    start_time = time.time()
                    except GeneratorExit:
                        pass
                    except:
                        pass
        # Use the generator object as the source of flask eventstream response
        binder_response = Response(generate(), mimetype='text/event-stream')
        # Fetch all the yielded messages
    binder_logs = binder_response.get_data(as_text=True)
    binder_logs = "".join(binder_logs)
    # After the upstream closes, check the server if there's 
    # a book built successfully.
    book_status = book_get_by_params(commit_hash=payload['commit_hash'])
    exec_error = book_execution_errored(owner,repo,provider,payload['commit_hash'])
    # For now, remove the block either way.
    # The main purpose is to avoid triggering
    # a build for the same request. Later on
    # you may choose to add dead time after a successful build.
    os.remove(lock_filename)
        # Append book-related response downstream
    if not book_status or exec_error:
        # These flags will determine how the response will be 
        # interpreted and returned outside the generator
        #gh_template_respond(github_client,"failure","Binder build has failed &#129344;",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], "The next comment will forward the logs")
        issue_comment = []
        msg = f"<p>&#129344; We ran into a problem building your book. Please see the log files below.</p><details><summary> <b>BinderHub build log</b> </summary><pre><code>{binder_logs}</code></pre></details><p>If the BinderHub build looks OK, please see the Jupyter Book build log(s) below.</p>"
        issue_comment.append(msg)
        owner,repo,provider = get_owner_repo_provider(payload['repo_url'],provider_full_name=True)
        # Retreive book build and execution report logs.
        book_logs = book_log_collector(owner,repo,provider,payload['commit_hash'])
        issue_comment.append(book_logs)
        msg = "<p>&#128030; After inspecting the logs above, you can interactively debug your notebooks on our <a href=\"https://test.conp.cloud\">BinderHub server</a>.</p> <p>For guidelines, please see <a href=\"https://docs.neurolibre.org/en/latest/TEST_SUBMISSION.html#debugging-for-long-neurolibre-submission\">the relevant documentation.</a></p>"
        issue_comment.append(msg)
        issue_comment = "\n".join(issue_comment)
        tmp_log = write_html_to_temp_directory(payload['commit_hash'], issue_comment)
        body = "<p>&#129344; We ran into a problem building your book. Please download the log file attached and open in your web browser.</p>"
        send_email_with_html_attachment_celery(payload['email'], payload['mail_subject'], body, tmp_log)

    else:
        #gh_template_respond(github_client,"success","Successfully built", payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], f"The next comment will forward the logs")
        #issue_comment = []
        mail_body = f"Book build successful: {book_status[0]['book_url']}"
        send_email_celery(payload['email'],payload['mail_subject'],mail_body)

def send_email_celery(to_email, subject, body):
    sg_api_key = os.getenv('SENDGRID_API_KEY')
    sender_email = "no-reply@neurolibre.org"

    message = Mail(
        from_email=sender_email,
        to_emails=to_email,
        subject=subject,
        html_content=body
    )

    try:
        sg = SendGridAPIClient(sg_api_key)
        response = sg.send(message)
        print("Email sent successfully!")
        print(response.status_code)
        print(response.body)
        print(response.headers)
    except Exception as e:
        print("Error sending email:", str(e))



def send_email_with_html_attachment_celery(to_email, subject, body, attachment_path):
    sg_api_key = os.getenv('SENDGRID_API_KEY')
    sender_email = "no-reply@neurolibre.org"

    message = Mail(
        from_email=sender_email,
        to_emails=to_email,
        subject=subject,
        html_content=body
    )

    with open(attachment_path, "rb") as file:
        data = file.read()

    encoded_data = base64.b64encode(data).decode()

    # Add the attachment to the email with MIME type "text/html"
    attachment = Attachment(
        FileContent(encoded_data),
        FileName(os.path.basename(attachment_path)),
        FileType("text/html"),
        Disposition("attachment")
    )
    message.attachment = attachment

    try:
        sg = SendGridAPIClient(sg_api_key)
        response = sg.send(message)
        print("Email sent successfully!")
        print(response.status_code)
        print(response.body)
        print(response.headers)
    except Exception as e:
        print("Error sending email:", str(e))

def write_html_to_temp_directory(commit_sha, logs):
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join("/DATA","api_build_logs", f"logs_{commit_sha[:7]}.html")

        with open(file_path, "w+") as f:
            f.write("<!DOCTYPE html>\n")
            f.write("<html lang=\"en\" class=\"no-js\">\n")
            f.write("<style>body { background-color: #fbdeda; color: black; font-family: monospace; } pre { background-color: #222222; border: none; color: white; padding: 10px; margin: 10px; overflow: auto; } code { font-family: monospace; font-size: 12px; background-color: #222222; color: white; border-radius: 5px; padding: 2px; } </style>\n")
            f.write("<body>\n")
            f.write(f"{logs}")
            f.write("</body></html>\n")
    
    return file_path