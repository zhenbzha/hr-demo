import "server-only";

import { AzureCliCredential, ManagedIdentityCredential } from "@azure/identity";
import { DataLakeServiceClient } from "@azure/storage-file-datalake";

import { config } from "@/lib/config";

const credential = config.isProduction
  ? new ManagedIdentityCredential({
      clientId: config.managedIdentityClientId,
    })
  : new AzureCliCredential({ tenantId: config.entraTenantId });

const serviceClient = new DataLakeServiceClient(
  config.storageDfsEndpoint,
  credential,
);

export const referenceFileSystem = serviceClient.getFileSystemClient(
  config.storageFileSystem,
);
