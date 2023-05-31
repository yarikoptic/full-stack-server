from celery import Celery
import time
import os 
import subprocess
from celery import states
import pytz
import datetime
from github_client import *
from common import *
from github import Github, UnknownObjectException
from dotenv import load_dotenv
import logging

DOI_PREFIX = "10.55458"
DOI_SUFFIX = "neurolibre"
JOURNAL_NAME = "NeuroLibre"
PAPERS_PATH = "https://neurolibre.org/papers"
PRODUCTION_BINDERHUB = "https://binder-mcgill.conp.cloud"

# Format logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Important, secrets will not be loaded otherwise.
load_dotenv()

# Setting Redis as both backend and broker
celery_app = Celery('neurolibre_celery_tasks', backend='redis://localhost:6379/1', broker='redis://localhost:6379/0')

celery_app.conf.update(task_track_started=True)

# Set timezone US/Eastern (Montreal)
def get_time():
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
def rsync_data(self, comment_id, issue_id, project_name, reviewRepository):
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
def rsync_book(self, repo_url, commit_hash, comment_id, issue_id, reviewRepository, server):
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
def fork_configure_repository(self, source_url, comment_id, issue_id, reviewRepository):
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