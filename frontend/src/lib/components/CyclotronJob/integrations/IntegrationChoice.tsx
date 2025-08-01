import { IconExternal, IconX } from '@posthog/icons'
import { LemonButton, LemonMenu, LemonSkeleton } from '@posthog/lemon-ui'
import { useActions, useValues } from 'kea'
import api from 'lib/api'
import { integrationsLogic } from 'lib/integrations/integrationsLogic'
import { IntegrationView } from 'lib/integrations/IntegrationView'
import { getIntegrationNameFromKind } from 'lib/integrations/utils'
import { ChannelSetupModal } from 'products/messaging/frontend/Channels/ChannelSetupModal'
import { urls } from 'scenes/urls'

import { CyclotronJobInputSchemaType } from '~/types'

export type IntegrationConfigureProps = {
    value?: number
    onChange?: (value: number | null) => void
    redirectUrl?: string
    schema?: CyclotronJobInputSchemaType
    integration?: string
    beforeRedirect?: () => void
}

export function IntegrationChoice({
    onChange,
    value,
    schema,
    integration,
    redirectUrl,
    beforeRedirect,
}: IntegrationConfigureProps): JSX.Element | null {
    const { integrationsLoading, integrations, newIntegrationModalKind } = useValues(integrationsLogic)
    const { newGoogleCloudKey, openNewIntegrationModal, closeNewIntegrationModal } = useActions(integrationsLogic)
    const kind = integration

    const integrationsOfKind = integrations?.filter((x) => x.kind === kind)
    const integrationKind = integrationsOfKind?.find((integration) => integration.id === value)

    if (!kind) {
        return null
    }

    if (integrationsLoading) {
        return <LemonSkeleton className="h-10" />
    }

    const kindName = getIntegrationNameFromKind(kind)

    function uploadKey(kind: string): void {
        const input = document.createElement('input')
        input.type = 'file'
        input.accept = '.json'
        input.onchange = async (e) => {
            const file = (e.target as HTMLInputElement).files?.[0]
            if (!file) {
                return
            }
            newGoogleCloudKey(kind, file, (integration) => onChange?.(integration.id))
        }
        input.click()
    }

    const button = (
        <LemonMenu
            items={[
                integrationsOfKind?.length
                    ? {
                          items: [
                              ...(integrationsOfKind?.map((integration) => ({
                                  icon: <img src={integration.icon_url} className="w-6 h-6 rounded" />,
                                  onClick: () => onChange?.(integration.id),
                                  active: integration.id === value,
                                  label: integration.display_name,
                              })) || []),
                          ],
                      }
                    : null,
                ['google-pubsub', 'google-cloud-storage'].includes(kind)
                    ? {
                          items: [
                              {
                                  onClick: () => uploadKey(kind),
                                  label: 'Upload Google Cloud .json key file',
                              },
                          ],
                      }
                    : ['email'].includes(kind)
                    ? {
                          items: [
                              {
                                  to: urls.messaging('channels'),
                                  label: 'Configure new email sender domain',
                              },
                          ],
                      }
                    : ['twilio'].includes(kind)
                    ? {
                          items: [
                              {
                                  label: 'Configure new Twilio account',
                                  onClick: () => openNewIntegrationModal('twilio'),
                              },
                          ],
                      }
                    : {
                          items: [
                              {
                                  to: api.integrations.authorizeUrl({
                                      kind,
                                      next: redirectUrl,
                                  }),
                                  disableClientSideRouting: true,
                                  onClick: beforeRedirect,
                                  label: integrationsOfKind?.length
                                      ? `Connect to a different integration for ${kindName}`
                                      : `Connect to ${kindName}`,
                              },
                          ],
                      },
                {
                    items: [
                        {
                            to: urls.settings('project-integrations'),
                            label: 'Manage integrations',
                            sideIcon: <IconExternal />,
                        },
                        value
                            ? {
                                  onClick: () => onChange?.(null),
                                  label: 'Clear',
                                  sideIcon: <IconX />,
                              }
                            : null,
                    ],
                },
            ]}
        >
            {integrationKind ? (
                <LemonButton type="secondary">Change</LemonButton>
            ) : (
                <LemonButton type="secondary">Choose {kindName} connection</LemonButton>
            )}
        </LemonMenu>
    )

    return (
        <>
            {integrationKind ? (
                <IntegrationView schema={schema} integration={integrationKind} suffix={button} />
            ) : (
                button
            )}

            <ChannelSetupModal
                isOpen={newIntegrationModalKind === 'twilio'}
                channelType="twilio"
                integration={integrationKind || undefined}
                onComplete={closeNewIntegrationModal}
            />
        </>
    )
}
