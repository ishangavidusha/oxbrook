# The Firebase emulator suite, for tests/firebase.py and examples/firebase.
#
# There is no official image. The emulators are a Node CLI, and the Firestore
# one runs on Java 21 or later, so this is both on Debian trixie, with
# firebase-tools pinned so a release cannot change what the suite tests against.
FROM node:22-trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
 && rm -rf /var/lib/apt/lists/*

ARG FIREBASE_TOOLS=15.34.0
RUN npm install -g firebase-tools@${FIREBASE_TOOLS} \
 && firebase setup:emulators:firestore \
 && firebase setup:emulators:database

WORKDIR /emulator
COPY firebase.json database.rules.json firestore.rules ./

# A `demo-` project needs no Google account and cannot reach real resources.
# Other `demo-` projects are accepted too (singleProjectMode is off), which is
# how each test run gets data of its own.
CMD ["firebase", "emulators:start", "--only", "firestore,database", "--project", "demo-oxbrook"]
