import { offset } from '@floating-ui/react'
import { IconArrowRight, IconStopFilled, IconWrench } from '@posthog/icons'
import { LemonButton, LemonTextArea, Tooltip } from '@posthog/lemon-ui'
import clsx from 'clsx'
import { useActions, useValues } from 'kea'
import { ReactNode, useState, useEffect } from 'react'
import React from 'react'
import { AIConsentPopoverWrapper } from 'scenes/settings/organization/AIConsentPopoverWrapper'

import { KeyboardShortcut } from '~/layout/navigation-3000/components/KeyboardShortcut'

import { maxGlobalLogic } from '../maxGlobalLogic'
import { maxLogic } from '../maxLogic'
import { maxThreadLogic } from '../maxThreadLogic'
import { ContextDisplay } from '../Context'
import { MAX_SLASH_COMMANDS, SlashCommandAutocomplete } from './SlashCommandAutocomplete'
import posthog from 'posthog-js'

interface QuestionInputProps {
    isFloating?: boolean
    isSticky?: boolean
    placeholder?: string
    children?: ReactNode
    contextDisplaySize?: 'small' | 'default'
    isThreadVisible?: boolean
    topActions?: ReactNode
    bottomActions?: ReactNode
    textAreaRef?: React.RefObject<HTMLTextAreaElement>
    containerClassName?: string
    onSubmit?: () => void
}

export const QuestionInput = React.forwardRef<HTMLDivElement, QuestionInputProps>(function BaseQuestionInput(
    {
        isFloating,
        isSticky,
        placeholder,
        children,
        contextDisplaySize,
        isThreadVisible,
        topActions,
        bottomActions,
        textAreaRef,
        containerClassName,
        onSubmit,
    },
    ref
) {
    const { tools, dataProcessingAccepted } = useValues(maxGlobalLogic)
    const { question } = useValues(maxLogic)
    const { setQuestion } = useActions(maxLogic)
    const { threadLoading, inputDisabled, submissionDisabledReason } = useValues(maxThreadLogic)
    const { askMax, stopGeneration, completeThreadGeneration } = useActions(maxThreadLogic)

    const [showAutocomplete, setShowAutocomplete] = useState(false)

    // Update autocomplete visibility when question changes
    useEffect(() => {
        const isSlashCommand = question[0] === '/'
        if (isSlashCommand && !showAutocomplete) {
            posthog.capture('Max slash command autocomplete shown')
        }
        setShowAutocomplete(isSlashCommand)
    }, [question, showAutocomplete])

    return (
        <div
            className={clsx(
                containerClassName,
                !isSticky && !isFloating
                    ? 'px-3 w-[min(40rem,100%)]'
                    : 'sticky bottom-0 z-10 w-full max-w-[45.25rem] self-center'
            )}
            ref={ref}
        >
            <div
                className={clsx(
                    'flex flex-col items-center',
                    isSticky &&
                        'mb-2 border border-[var(--border-primary)] rounded-lg backdrop-blur-sm bg-[var(--glass-bg-3000)]'
                )}
            >
                <div className="relative w-full flex flex-col">
                    {children}
                    <div
                        className={clsx(
                            'flex flex-col',
                            'border border-[var(--border-primary)] rounded-[var(--radius)]',
                            'bg-[var(--bg-fill-input)]',
                            'hover:border-[var(--border-bold)] focus-within:border-[var(--border-bold)]',
                            isFloating && 'border-primary m-1'
                        )}
                        onClick={(e) => {
                            // If user clicks anywhere with the area with a hover border, activate input - except on button clicks
                            if (!(e.target as HTMLElement).closest('button')) {
                                textAreaRef?.current?.focus()
                            }
                        }}
                    >
                        {!isThreadVisible ? (
                            <div className="flex items-start justify-between">
                                <ContextDisplay size={contextDisplaySize} />
                                <div className="flex items-start gap-1 h-full mt-1 mr-1">{topActions}</div>
                            </div>
                        ) : (
                            <ContextDisplay size={contextDisplaySize} />
                        )}

                        <SlashCommandAutocomplete
                            onActivate={(command) => {
                                if (command.arg) {
                                    setQuestion(command.name + ' ') // Rest must be filled in by the user
                                } else {
                                    askMax(command.name)
                                }
                            }}
                            onSelect={(command) =>
                                command.arg ? setQuestion(command.name + ' ') : setQuestion(command.name)
                            }
                            visible={showAutocomplete}
                            onClose={() => setShowAutocomplete(false)}
                            searchText={question}
                        >
                            <LemonTextArea
                                ref={textAreaRef}
                                value={question}
                                onChange={setQuestion}
                                placeholder={
                                    threadLoading
                                        ? 'Thinking…'
                                        : isFloating
                                        ? placeholder || 'Ask follow-up (/ for commands)'
                                        : 'Ask away (/ for commands)'
                                }
                                onPressEnter={() => {
                                    if (question && !submissionDisabledReason && !threadLoading) {
                                        onSubmit?.()
                                        askMax(question)
                                    }
                                }}
                                disabled={inputDisabled}
                                minRows={1}
                                maxRows={10}
                                className="!border-none !bg-transparent min-h-0 py-2.5 pl-2.5 pr-12"
                            />
                        </SlashCommandAutocomplete>
                    </div>
                    <div
                        className={clsx('absolute flex items-center', {
                            'bottom-[11px] right-3': isFloating,
                            'bottom-[7px] right-2': !isFloating,
                        })}
                    >
                        <AIConsentPopoverWrapper
                            placement="bottom-end"
                            showArrow
                            onApprove={() => askMax(question)}
                            onDismiss={() => completeThreadGeneration()}
                            middleware={[
                                offset((state) => ({
                                    mainAxis: state.placement.includes('top') ? 30 : 1,
                                })),
                            ]}
                            hidden={!threadLoading}
                        >
                            <LemonButton
                                type={(isFloating && !question) || threadLoading ? 'secondary' : 'primary'}
                                onClick={() => {
                                    if (threadLoading) {
                                        stopGeneration()
                                    } else {
                                        askMax(question)
                                    }
                                }}
                                tooltip={
                                    threadLoading ? (
                                        "Let's bail"
                                    ) : (
                                        <>
                                            Let's go! <KeyboardShortcut enter />
                                        </>
                                    )
                                }
                                loading={threadLoading && !dataProcessingAccepted}
                                disabledReason={
                                    threadLoading && !dataProcessingAccepted
                                        ? 'Pending approval'
                                        : submissionDisabledReason
                                }
                                size="small"
                                icon={
                                    threadLoading ? (
                                        <IconStopFilled />
                                    ) : (
                                        MAX_SLASH_COMMANDS.find((cmd) => cmd.name === question.split(' ', 1)[0])
                                            ?.icon || <IconArrowRight />
                                    )
                                }
                            />
                        </AIConsentPopoverWrapper>
                    </div>
                </div>
                <div className="flex items-center w-full gap-1 justify-center">
                    {tools.length > 0 && (
                        <div
                            className={clsx(
                                'flex flex-wrap gap-x-1 gap-y-0.5 text-xs font-medium cursor-default px-1.5 whitespace-nowrap',
                                !isFloating
                                    ? 'w-[calc(100%-1rem)] py-1 border-x border-b rounded-b backdrop-blur-sm bg-[var(--glass-bg-3000)]'
                                    : `w-full pb-1`
                            )}
                        >
                            <span>Tools here:</span>
                            {tools.map((tool) => (
                                <Tooltip key={tool.name} title={tool.description}>
                                    <i className="flex items-center gap-1 cursor-help">
                                        {tool.icon || <IconWrench />}
                                        {tool.displayName}
                                    </i>
                                </Tooltip>
                            ))}
                        </div>
                    )}
                    {bottomActions && <div className="ml-auto">{bottomActions}</div>}
                </div>
            </div>
        </div>
    )
})
