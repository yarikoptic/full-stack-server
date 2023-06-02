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


DOI_PREFIX = "10.55458"
DOI_SUFFIX = "neurolibre"
JOURNAL_NAME = "NeuroLibre"
PAPERS_PATH = "https://neurolibre.org/papers"
PRODUCTION_BINDERHUB = "https://binder-mcgill.conp.cloud"

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
                    messages.append(message)
                    elapsed_time = time.time() - start_time
                    # Update issue every two minutes
                    if elapsed_time >= 120:
                        n_updates = n_updates + 1
                        gh_template_respond(github_client,"started",payload['task_title'] + f" {n_updates*2} minutes passed",payload['review_repository'],payload['issue_id'],task_id,payload['comment_id'], messages)
                        start_time = time.time()
                    # To the response.
                    yield message
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
    if response.ok:
        # Create binder_stream generator object
        generator = binder_stream(response, github_client,lock_filename, task_id, payload)
        # Use the generator object as the source of flask eventstream response
        binder_response = Response(generator, mimetype='text/event-stream')
        # Fetch all the yielded messages
        binder_logs = binder_response.get_data(as_text=True)

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
            issue_comment = []
            msg = f"<p>&#129344; We ran into a problem building your book. Please see the log files below.</p><details><summary> <b>BinderHub build log</b> </summary><pre><code>{binder_logs}</code></pre></details><p>If the BinderHub build looks OK, please see the Jupyter Book build log(s) below.</p>"
            issue_comment.append(msg)
            owner,repo,provider = get_owner_repo_provider(payload['repo_url'],provider_full_name=True)
            # Retreive book build and execution report logs.
            book_logs = book_log_collector(owner,repo,provider,payload['commit_hash'])
            issue_comment.append(book_logs)
            msg = "<p>&#128030; After inspecting the logs above, you can interactively debug your notebooks on our <a href=\"https://binder.conp.cloud\">BinderHub server</a>.</p> <p>For guidelines, please see <a href=\"https://docs.neurolibre.org/en/latest/TEST_SUBMISSION.html#debugging-for-long-neurolibre-submission\">the relevant documentation.</a></p>"
            issue_comment.append(msg)
            # Send a new comment
            gh_create_comment(github_client, payload['review_repository'],payload['issue_id'],issue_comment)
        else:
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

    owner,repo,provider = get_owner_repo_provider(payload['repo_url'],provider_full_name=True)
    
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
    
